import { NavLink, Outlet } from "react-router-dom";

const nav = [
  { to: "/", label: "Dashboard", end: true },
  { to: "/workflows", label: "Workflows", end: false },
  { to: "/monitoring", label: "Monitoring", end: false },
  { to: "/settings", label: "Settings", end: false },
];

export default function Layout() {
  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-edge bg-panel/60">
        <div className="mx-auto flex max-w-6xl items-center gap-6 px-6 py-3">
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
      <main className="mx-auto w-full max-w-6xl flex-1 px-6 py-6">
        <Outlet />
      </main>
    </div>
  );
}
