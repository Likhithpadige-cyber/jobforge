"use client";

import { useEffect, useState } from "react";

function localGreeting(): string {
  try {
    const hour = new Date().getHours();
    if (!Number.isInteger(hour) || hour < 0 || hour > 23) return "Welcome back";
    if (hour < 12) return "Good morning";
    if (hour < 18) return "Good afternoon";
    return "Good evening";
  } catch {
    return "Welcome back";
  }
}

export function DashboardGreeting({ name }: { name: string }) {
  const [greeting, setGreeting] = useState("Welcome back");

  useEffect(() => {
    const updateGreeting = () => setGreeting(localGreeting());
    updateGreeting();
    const timer = window.setInterval(updateGreeting, 60_000);
    return () => window.clearInterval(timer);
  }, []);

  return <>{greeting}, {name}.</>;
}
