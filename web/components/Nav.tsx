"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const LINKS = [
  { href: "/", label: "Runs" },
  { href: "/evals", label: "Evaluations" },
];

export function Nav() {
  const path = usePathname();
  return (
    <nav aria-label="Main">
      {LINKS.map((link) => {
        const active = link.href === "/" ? path === "/" || path.startsWith("/runs") : path.startsWith(link.href);
        return (
          <Link key={link.href} href={link.href} aria-current={active ? "page" : undefined}>
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}
