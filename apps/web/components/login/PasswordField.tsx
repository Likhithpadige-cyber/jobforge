"use client";

/**
 * The password field and its reveal control.
 *
 * The toggle is a real button with a changing accessible name, not an icon
 * that only reads as a picture: a screen-reader user needs to know both that
 * the control exists and which state it is in. `aria-pressed` carries the
 * state; the name says what pressing it will do.
 *
 * Revealing is local and transient. It never leaves this component, and the
 * page clears `value` on a successful sign-in regardless of what is showing.
 */

import { useState } from "react";
import { FIELD_INPUT_CLASS, LockGlyph, LoginField } from "./LoginField";

export function PasswordField({
  value,
  onChange,
}: {
  value: string;
  onChange: (next: string) => void;
}) {
  const [revealed, setRevealed] = useState(false);

  return (
    <LoginField htmlFor="password" label="Password" glyph={<LockGlyph />}>
      <input
        id="password"
        name="password"
        type={revealed ? "text" : "password"}
        autoComplete="current-password"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={`${FIELD_INPUT_CLASS} pr-11`}
      />
      <button
        type="button"
        onClick={() => setRevealed((shown) => !shown)}
        aria-pressed={revealed}
        aria-controls="password"
        className="absolute right-2 top-1/2 -translate-y-1/2 rounded-chip p-2 text-ink-soft transition hover:bg-inset hover:text-accent"
      >
        <span className="sr-only">{revealed ? "Hide password" : "Show password"}</span>
        <svg
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          className="h-[18px] w-[18px]"
          aria-hidden="true"
          focusable="false"
        >
          <path d="M2.5 12S6 5.75 12 5.75 21.5 12 21.5 12 18 18.25 12 18.25 2.5 12 2.5 12Z" />
          <circle cx="12" cy="12" r="2.75" />
          {!revealed && <path d="m4.5 4.5 15 15" />}
        </svg>
      </button>
    </LoginField>
  );
}
