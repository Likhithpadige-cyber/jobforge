"use client";
import "./globals.css";
import { fontVariables } from "./fonts";
import { ErrorFallback } from "@/components/ErrorFallback";
export default function GlobalError({ reset }: { reset: () => void }) {
  return <html lang="en" className={fontVariables}><body><ErrorFallback reset={reset} /></body></html>;
}
