import { Brand } from "../Brand";

/** Same shared ribbon geometry as the sidebar, with the existing tagline. */
export function BrandLockup({ size = "card" }: { size?: "corner" | "card" }) {
  return <span className="inline-flex flex-col items-center">
    <Brand size={size} />
    <span className={`mt-1 font-medium uppercase text-ink-soft ${size === "corner" ? "text-[0.625rem] sm:text-[0.6875rem] tracking-[0.42em]" : "text-[0.625rem] tracking-[0.34em]"}`}>Build Apply Grow</span>
  </span>;
}
