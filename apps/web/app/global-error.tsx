"use client";
import "./globals.css";
import { ErrorFallback } from "@/components/ErrorFallback";
export default function GlobalError({ reset }: { reset: () => void }) {
  return <html lang="en"><body><ErrorFallback reset={reset} /></body></html>;
}
