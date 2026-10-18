"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { AuthGate, useIdentity } from "@/components/auth";

const NAV = [
  { href: "/talk", label: "Talk" },
  { href: "/memory", label: "Memory" },
  { href: "/insights", label: "Insights" },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <AppBar />
      <main>{children}</main>
    </AuthGate>
  );
}

function AppBar() {
  const pathname = usePathname();
  const { identity, signOut } = useIdentity();

  return (
    <header className="app-bar">
      <Link href="/" className="wordmark">
        Haven
      </Link>
      <nav className="app-nav" aria-label="Main">
        {NAV.map((item) => (
          <Link key={item.href} href={item.href} aria-current={pathname.startsWith(item.href) ? "page" : undefined}>
            {item.label}
          </Link>
        ))}
      </nav>
      {identity && (
        <div className="account">
          <span className="account-name" title={identity.customerId}>
            {identity.mode === "guest" ? "Guest" : identity.label}
          </span>
          <button onClick={() => void signOut()}>Sign out</button>
        </div>
      )}
    </header>
  );
}
