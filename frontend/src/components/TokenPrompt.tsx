/** Modal prompt for the shared API token (Issue #8). */

import { FormEvent, useEffect, useRef, useState } from "react";

export interface TokenPromptProps {
  /** Called with the trimmed token after the user submits. */
  onSubmit: (token: string) => void;
  /** Present only when the user can dismiss the dialog (a token exists). */
  onCancel?: () => void;
}

export function TokenPrompt({ onSubmit, onCancel }: TokenPromptProps) {
  const [value, setValue] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = value.trim();
    if (trimmed) onSubmit(trimmed);
  };

  return (
    <div className="auth-overlay no-print" role="dialog" aria-modal="true">
      <form className="auth-modal" onSubmit={handleSubmit}>
        <h2>アクセストークン</h2>
        <p>
          このサーバーはトークン認証が有効です。
          管理者が設定したアクセストークンを入力してください。
        </p>
        <input
          ref={inputRef}
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="アクセストークン"
          autoComplete="off"
        />
        <div className="auth-actions">
          {onCancel && (
            <button
              type="button"
              className="btn"
              onClick={onCancel}
            >
              キャンセル
            </button>
          )}
          <button
            type="submit"
            className="btn btn-primary"
            disabled={!value.trim()}
          >
            接続
          </button>
        </div>
      </form>
    </div>
  );
}
