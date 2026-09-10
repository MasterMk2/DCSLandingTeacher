-- dlt-capture-runways.lua -- capture runway geometry straight out of DCS.
--
-- Drop into  <Saved Games>/<DCS>/Scripts/Hooks/  and load any mission. Once
-- the mission is loaded it records every airdrome on the map and every runway
-- DCS reports for it, then scripts/dlt_runways_from_dump.py turns that into a
-- config/runways/ seed.
--
-- Why this exists: runway geometry only exists while the map is loaded (the
-- terrain's own airfield config is encrypted), and the usual route to it --
-- DCSServerBot's /airbase endpoint -- only answers for a server the bot
-- launched itself. This route needs nothing but DCS.
--
-- Recorded per airbase: id, name, lat, lng, alt, position{x,y,z}, runwayList
-- (the shape DCSServerBot's REST API returns, so both parse the same way).
-- Recorded per runway: Name, course, position{x,y,z}, length, width -- and,
-- because only DCS can do it exactly, the runway centre and a probe point
-- 1000 grid metres along the course converted by coord.LOtoLL:
--   lat, lng, probe_lat, probe_lng, probe_grid_m
-- DCS x/z is a transverse-Mercator grid. Converting it to lat/lon on the
-- outside means approximating that projection; doing it with meridian
-- convergence alone left thresholds up to 18 m out on Caucasus, growing with
-- distance from the central meridian (the scale factor). With LOtoLL there is
-- nothing left to approximate: the centre is DCS's own lat/lon, the probe gives
-- the true heading and the local grid-to-ground scale.
--
-- WHERE THE DATA IS. getRunways(), coalition and coord live in the mission
-- SCRIPTING environment. The 'mission' Lua state a hook reaches with
-- net.dostring_in is not that environment: running the enumeration there
-- fails on the first global ("attempt to index global 'coalition'") and DCS
-- hands the error text back as if it were the result. The 'server' state is
-- the scripting environment and returns the dump in one piece. Where it is not
-- available, the fallback enters the scripting environment the way
-- DCSServerBot does -- net.dostring_in('mission', 'a_do_script(...)') -- which
-- returns nothing, so that side reports through env.info, one airbase per
-- dcs.log line:
--   DLT-RWY-BEGIN <theatre> / DLT-RWY {airbase json} ... / DLT-RWY-END <n>

local dltCapture = {}

-- The enumeration, shared by both routes. Leaves `theatre` and `items` set.
local ENUM = [==[
local function esc(s)
    s = tostring(s)
    s = s:gsub('\\', '\\\\'):gsub('"', '\\"')
    s = s:gsub('\n', '\\n'):gsub('\r', '\\r'):gsub('\t', '\\t')
    return s
end

local function num(n)
    if n == nil then return 'null' end
    return string.format('%.9g', n)
end

-- Latitude/longitude need more digits than %.9g gives a three-digit
-- longitude: six decimals is ~9 cm, and the heading comes from a point only a
-- kilometre away.
local function ll(n)
    if n == nil then return 'null' end
    return string.format('%.12g', n)
end

local PROBE = 1000

local items = {}
local seen = {}
local sides = { coalition.side.NEUTRAL, coalition.side.RED, coalition.side.BLUE }
for _, side in pairs(sides) do
    local ok, bases = pcall(coalition.getAirbases, side)
    if ok and bases then
        for _, ab in pairs(bases) do
            local okName, name = pcall(function() return ab:getName() end)
            if okName and name and not seen[name] then
                seen[name] = true
                local okRw, runways = pcall(function() return ab:getRunways() end)
                -- Only real airdromes have runways; FARPs and carriers do not,
                -- and neither belongs in a runway table.
                if okRw and runways and #runways > 0 then
                    local p = ab:getPoint()
                    local lat, lon, alt = coord.LOtoLL(p)
                    local rw, names = {}, {}
                    for _, r in pairs(runways) do
                        local rp = r.position or {}
                        local rname = esc(r.Name or r.name or '')
                        local clat, clon, plat, plon = nil, nil, nil, nil
                        if rp.x and rp.z and r.course then
                            local cy = rp.y or 0
                            clat, clon = coord.LOtoLL({ x = rp.x, y = cy, z = rp.z })
                            -- DCS reports course as the NEGATED grid heading;
                            -- x is grid north and z grid east.
                            local gh = -r.course
                            plat, plon = coord.LOtoLL({
                                x = rp.x + math.cos(gh) * PROBE,
                                y = cy,
                                z = rp.z + math.sin(gh) * PROBE,
                            })
                        end
                        rw[#rw + 1] = string.format(
                            '{"Name":"%s","course":%s,"length":%s,"width":%s,' ..
                            '"position":{"x":%s,"y":%s,"z":%s},' ..
                            '"lat":%s,"lng":%s,"probe_lat":%s,"probe_lng":%s,' ..
                            '"probe_grid_m":%d}',
                            rname, num(r.course), num(r.length), num(r.width),
                            num(rp.x), num(rp.y), num(rp.z),
                            ll(clat), ll(clon), ll(plat), ll(plon), PROBE)
                        names[#names + 1] = '"' .. rname .. '"'
                    end
                    items[#items + 1] = string.format(
                        '{"id":"%s","name":"%s","lat":%s,"lng":%s,"alt":%s,' ..
                        '"position":{"x":%s,"y":%s,"z":%s},' ..
                        '"runwayList":[%s],"runways":[%s]}',
                        esc(name), esc(name), ll(lat), ll(lon), num(alt),
                        num(p.x), num(p.y), num(p.z),
                        table.concat(names, ','), table.concat(rw, ','))
                end
            end
        end
    end
end
local theatre = esc(env.mission.theatre)
]==]

local RETURN_TAIL = [==[
return string.format('{"theatre":"%s","airbases":[%s]}',
                     theatre, table.concat(items, ','))
]==]

local LOG_TAIL = [==[
env.info('DLT-RWY-BEGIN ' .. theatre)
for _, item in ipairs(items) do
    env.info('DLT-RWY ' .. item)
end
env.info('DLT-RWY-END ' .. tostring(#items))
]==]

local function note(msg)
    log.write('DLT', log.INFO, msg)
end

local function write(path, text)
    local handle = io.open(path, 'w')
    if not handle then
        note('cannot open ' .. path)
        return false
    end
    handle:write(text)
    handle:close()
    return true
end

-- A real dump, not an error message DCS returned in its place.
local function looks_like_dump(s)
    return type(s) == 'string'
        and s:sub(1, 12) == '{"theatre":"'
        and not s:find('"airbases":%[%]')
end

local captured = false

local function capture(when)
    if captured then return end

    local ok, result = pcall(net.dostring_in, 'server', ENUM .. RETURN_TAIL)
    if ok and looks_like_dump(result) then
        local path = lfs.writedir() .. 'Logs/dlt-runways.json'
        if write(path, result) then
            captured = true
            note('runway capture written via server state at ' .. when ..
                 ' (' .. tostring(#result) .. ' bytes)')
            return
        end
    end
    note('server state gave no dump at ' .. when .. ': ' ..
         tostring(result):sub(1, 160))

    -- Fallback: the scripting environment, reporting through dcs.log. Issued
    -- at both callbacks; the reader takes the last block that has airbases in
    -- it, so an early, empty one does no harm.
    local code = ENUM .. LOG_TAIL
    local ok2, res2 = pcall(net.dostring_in, 'mission',
                            'a_do_script(' .. string.format('%q', code) .. ')')
    note('a_do_script route issued at ' .. when .. ': ' .. tostring(ok2) ..
         ' ' .. tostring(res2):sub(1, 160))
end

-- Both, because a dedicated server configured with pause_on_load = true
-- loads the mission and then sits paused: onSimulationStart never fires, and
-- a hook listening only for it waits forever.
function dltCapture.onMissionLoadEnd()
    capture('onMissionLoadEnd')
end

function dltCapture.onSimulationStart()
    capture('onSimulationStart')
end

DCS.setUserCallbacks(dltCapture)
