"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { Leaf, Microphone, Brain, ChartLineUp, House, SignOut, UserCircle } from "@phosphor-icons/react";
import { AuthGate, useIdentity } from "@/components/auth";

const NAV = {
  companion: [
    { href: "/talk", label: "Talk", icon: <Microphone size={18} weight="duotone" /> },
    { href: "/memory", label: "Memory", icon: <Brain size={18} weight="duotone" /> },
  ],
  system: [{ href: "/insights", label: "Insights", icon: <ChartLineUp size={18} weight="duotone" /> }],
} as const;

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <AuthGate>
      <ConsoleShell>{children}</ConsoleShell>
    </AuthGate>
  );
}

function AccountChip({ compact = false }: { compact?: boolean }) {
  const { identity, signOut } = useIdentity();
  if (!identity) return null;
  return (
    <div
      style={{
        padding: compact ? "8px 12px" : "12px 10px",
        borderTop: "1px solid var(--line)",
        fontSize: 12,
        color: "var(--text-mid)",
        display: "flex",
        flexDirection: "column",
        gap: 6,
      }}
    >
      <span
        style={{ display: "flex", alignItems: "center", gap: 7, minWidth: 0 }}
      >
        <UserCircle size={15} weight="duotone" color="var(--signal)" />
        <span
          style={{
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
          title={identity.customerId}
        >
          {identity.label}
          {identity.mode === "guest" ? "" : ""}
        </span>
      </span>
      <button
        onClick={() => void signOut()}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          border: "1px solid var(--line)",
          background: "var(--ink-2)",
          borderRadius: 7,
          padding: "5px 9px",
          fontSize: 11.5,
          cursor: "pointer",
          color: "var(--text-hi)",
          alignSelf: "flex-start",
        }}
      >
        <SignOut size={13} /> Sign out
      </button>
    </div>
  );
}

function ConsoleShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();

  const isActive = (href: string) =>
    pathname === href || pathname.startsWith(`${href}/`);

  const NavList = (
    <>
      {Object.entries(NAV).map(([section, items]) => (
        <div key={section}>
          <div className="sidebar-section">{section}</div>
          {items.map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`sidebar-item ${isActive(item.href) ? "active" : ""}`}
              aria-current={isActive(item.href) ? "page" : undefined}
            >
              {item.icon}
              {item.label}
            </Link>
          ))}
        </div>
      ))}
    </>
  );

  return (
    <div className="shell">
      {/* ── Desktop sidebar ── */}
      <aside className="sidebar desktop-only">
        <div className="sidebar-brand">
          <Leaf size={20} color="var(--signal)" weight="fill" />
          <span>HAVEN</span>
        </div>
        <nav className="sidebar-nav">{NavList}</nav>
        <div
          style={{ padding: "12px 10px", borderTop: "1px solid var(--line)" }}
        >
          <Link href="/" className="sidebar-item">
            <House size={17} weight="duotone" />
            Back to Home
          </Link>
        </div>
        <AccountChip />
      </aside>

      {/* ── Mobile top bar ── */}
      <div className="mobile-bar mobile-only">
        <div className="sidebar-brand">
          <Link
            href="/"
            style={{
              display: "flex",
              alignItems: "center",
              gap: 9,
              textDecoration: "none",
              color: "inherit",
            }}
          >
            <Leaf size={19} color="var(--signal)" weight="fill" />
            <span>HAVEN</span>
          </Link>
        </div>
        <nav
          style={{
            display: "flex",
            flexDirection: "column",
            borderTop: "1px solid var(--line)",
          }}
        >
          {[...NAV.companion, ...NAV.system].map((item) => (
            <Link
              key={item.href}
              href={item.href}
              className={`sidebar-item ${isActive(item.href) ? "active" : ""}`}
              aria-current={isActive(item.href) ? "page" : undefined}
            >
              {item.icon}
              {item.label}
            </Link>
          ))}
          <AccountChip compact />
        </nav>
      </div>

      {/* ── Main content ── */}
      <main
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
        }}
      >
        {children}
      </main>
    </div>
  );
}
