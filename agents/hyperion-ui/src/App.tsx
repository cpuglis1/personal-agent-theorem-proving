/**
 * App.tsx — Root component and client-side route table for the Hyperion UI.
 *
 * Role in the system:
 *   This is the top-level React component for the Hyperion web console (served on
 *   :4102), the front-end for the Hyperion multi-agent orchestrator (FastAPI on
 *   :4100). It defines the application's URL routing using react-router-dom and
 *   maps each path to a page component. The router itself (BrowserRouter/HashRouter)
 *   is expected to be mounted higher up (e.g. in main.tsx), so this file only
 *   declares the <Routes>/<Route> tree.
 *
 * Layout / nesting:
 *   All routes are nested inside a single parent <Route element={<Layout />}>.
 *   Layout renders the shared chrome (navigation, header) and an <Outlet /> where
 *   the matched child page is injected. The index route (path "/") renders the
 *   Dashboard.
 *
 * Route conventions:
 *   - ":id" / ":name" segments are dynamic params read by the page via useParams.
 *   - The same editor component is reused for "new" and "edit" flows
 *     (e.g. AgentEditor handles both "agents/new" and "agents/:id"); the page
 *     distinguishes create vs. edit by the presence of the :id param.
 *   - "runs/:id/trace" renders TraceFlow, a graph/flow visualization of a run's
 *     agent execution trace, distinct from the textual RunDetail view.
 */
import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import AgentEditor from "./pages/AgentEditor";
import Settings from "./pages/Settings";
import RunDetail from "./pages/RunDetail";
import Runs from "./pages/Runs";
import Monitoring from "./pages/Monitoring";
import Workflows from "./pages/Workflows";
import WorkflowEditor from "./pages/WorkflowEditor";
import TraceFlow from "./pages/TraceFlow";
import ProverRun from "./pages/ProverRun";
import ProverSubmit from "./pages/ProverSubmit";

/**
 * App — Root component that declares the Hyperion UI route table.
 *
 * Renders a single <Routes> tree. All pages are nested under the shared <Layout>
 * route so they inherit the common navigation/header chrome via Layout's <Outlet>.
 *
 * @returns The application's route configuration element.
 */
export default function App() {
  return (
    <Routes>
      <Route element={<Layout />}>
        <Route index element={<Dashboard />} />
        <Route path="agents/new" element={<AgentEditor />} />
        <Route path="agents/:id" element={<AgentEditor />} />
        <Route path="workflows" element={<Workflows />} />
        <Route path="workflows/new" element={<WorkflowEditor />} />
        <Route path="workflows/:id" element={<WorkflowEditor />} />
        <Route path="monitoring" element={<Monitoring />} />
        <Route path="settings" element={<Settings />} />
        <Route path="runs" element={<Runs />} />
        <Route path="runs/:id" element={<RunDetail />} />
        <Route path="runs/:id/trace" element={<TraceFlow />} />
        <Route path="prover" element={<ProverRun />} />
        <Route path="prover/submit" element={<ProverSubmit />} />
        <Route path="prover/runs/:id" element={<ProverRun />} />
      </Route>
    </Routes>
  );
}
