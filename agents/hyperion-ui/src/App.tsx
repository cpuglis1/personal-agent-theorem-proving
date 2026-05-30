import { Routes, Route } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import AgentEditor from "./pages/AgentEditor";
import Settings from "./pages/Settings";
import RunDetail from "./pages/RunDetail";
import Monitoring from "./pages/Monitoring";
import Workflows from "./pages/Workflows";
import WorkflowEditor from "./pages/WorkflowEditor";

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
        <Route path="runs/:id" element={<RunDetail />} />
      </Route>
    </Routes>
  );
}
