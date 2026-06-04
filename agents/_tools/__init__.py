# ~/ai/agents/_tools/__init__.py
"""Package marker for ``agents/_tools``.

Purpose
-------
This file makes ``agents/_tools`` an importable Python package. It is the
shared home for tool wrappers used across the workspace — in particular the
Open WebUI (OWUI) tool plugins that adapt local services (Hyperion, the
second-brain Qdrant index, etc.) into the OWUI tool-calling format.

Role in the system
------------------
``agents/_tools`` sits alongside ``agents/_shared`` (low-level Qdrant/Notion
clients). Where ``_shared`` holds reusable client libraries, ``_tools`` holds
the user-facing tool/affordance wrappers that those clients power. Modules in
this package are imported by the various agent projects and by the OWUI tool
loader.

Notes
-----
- Intentionally empty of code: it carries no package-level initialization,
  re-exports, or side effects. Keeping it bare avoids import-time surprises
  (e.g. eager network/Qdrant connections) when any submodule is imported.
- Add submodules (one tool per module) rather than putting logic here. If
  curated re-exports become useful later, define ``__all__`` and import the
  submodules explicitly so the public surface stays intentional.
"""
