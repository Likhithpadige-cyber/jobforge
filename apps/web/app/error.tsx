"use client";
import { ErrorFallback } from "@/components/ErrorFallback";
export default function ErrorPage({ reset }: { reset: () => void }) {
  return <ErrorFallback reset={reset} />;
}
