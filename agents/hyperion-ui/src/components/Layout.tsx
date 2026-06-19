/**
 * Layout.tsx — Top-level application chrome for the Hyperion web console.
 *
 * Role in the system:
 *   This component is the shared shell rendered around every routed page of the
 *   Hyperion UI (React + TypeScript + Vite + Tailwind, served on :4102). It is
 *   typically mounted as the parent route in the router configuration so that all
 *   child pages render into its <Outlet />. It provides:
 *     - A persistent top header with the "Hyperion" brand mark.
 *     - A primary navigation bar linking to the console's main sections.
 *     - A main content region where the active child route is rendered.
 *
 * Design notes:
 *   - Navigation entries are declared once in the module-level `nav` array so the
 *     menu is data-driven; adding a section means adding one entry here (provided
 *     a matching <Route> exists in the router).
 *   - The `end` flag controls react-router's active-link matching. The Dashboard
 *     link uses `end: true` so it is only highlighted on the exact "/" path and
 *     not for every nested route (which all start with "/"). Other links use
 *     prefix matching so they stay active on their sub-routes (e.g. /runs/:id).
 *   - Styling uses Tailwind utility classes plus a few project-specific theme
 *     tokens (`border-edge`, `bg-panel`, `bg-edge`) defined in the Tailwind config.
 */
import { NavLink, Outlet } from "react-router-dom";

/**
 * Static, ordered list of primary navigation destinations rendered in the header.
 *
 * Each entry:
 *   - to:    Router path the link navigates to (must match a configured <Route>).
 *   - label: Human-readable text shown in the nav bar.
 *   - end:   When true, the link is only marked active on an exact path match;
 *            when false, it stays active for any descendant path. See file header.
 */
const nav = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/runs", label: "Runs", end: false },
  { to: "/prover", label: "Prover", end: false },
  { to: "/workflows", label: "Workflows", end: false },
  { to: "/monitoring", label: "Monitoring", end: false },
  { to: "/settings", label: "Settings", end: false },
];

/**
 * Layout — Application shell component.
 *
 * Renders the persistent header (brand + navigation) and a main region that
 * hosts the currently matched child route via react-router's <Outlet />. Intended
 * to be used as the element of a parent route wrapping all page routes.
 *
 * @returns The JSX for the full-height app chrome surrounding the active page.
 */
export default function Layout() {
  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-edge bg-panel/60">
        <div className="flex items-center gap-6 px-6 py-3">
          <span className="text-lg font-bold tracking-tight text-sky-300">Hyperion</span>
          <nav className="flex gap-1">
            {nav.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={({ isActive }) =>
                  `rounded-md px-3 py-1.5 text-sm font-medium ${
                    isActive ? "bg-edge text-sky-200" : "text-slate-400 hover:text-slate-200"
                  }`
                }
              >
                {n.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>
      <main className="w-full flex-1 px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
