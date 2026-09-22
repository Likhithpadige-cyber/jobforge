/**
 * The frosted card the form sits in.
 *
 * Two things are deliberate. The surface is near-opaque rather than the
 * thinner glass the reference suggests, because body text has to clear 7:1
 * against whatever the artwork puts behind it and a translucent card cannot
 * promise that. And `backdrop-blur` is an enhancement only: where it is
 * unsupported the card is still a solid white panel.
 *
 * Width comes from the stage, not from here - the card has to relate to the
 * brick pillar behind it, and the stage is what knows how wide that is.
 */

import type { ReactNode } from "react";

export function LoginCard({ children }: { children: ReactNode }) {
  return (
    <div className="relative w-full">
      {/* The glow. Its own element so it can bleed past the card's edges
          without the card needing an oversized shadow. */}
      <div
        aria-hidden="true"
        className="absolute -inset-4 rounded-[2.5rem] bg-[radial-gradient(62%_62%_at_50%_50%,rgba(100,87,181,0.34),transparent_72%)] blur-2xl"
      />
      <div className="relative rounded-[1.75rem] border border-white/75 bg-white/[0.90] p-5 shadow-[0_28px_70px_-28px_rgba(48,54,86,0.6)] backdrop-blur-xl sm:p-6">
        {children}
      </div>
    </div>
  );
}
