# SDD Artifact Pack Manifest

Status: `selected`

## Selected Packs

### core-baseline
- Title: Core SDD Baseline
- Lifecycle: `builtin`
- Score: `1.0`

### architecture
- Title: Architecture Baseline
- Lifecycle: `builtin`
- Score: `1.0`
- Evidence:
  - `.cli-proxy/.codebase_map/ARCHITECTURE.md` via `architecture_doc` (Architecture document found.)
  - `.cli-proxy/.codebase_map` via `codebase_architecture_doc` (Codebase map contains architecture context.)

### asyncapi
- Title: Event-Driven API / AsyncAPI
- Lifecycle: `builtin`
- Score: `1.0`
- Evidence:
  - `modes/sdd/packs/builtin/asyncapi.yaml` via `asyncapi_file` (AsyncAPI YAML contract found.)

### openapi
- Title: HTTP API / OpenAPI
- Lifecycle: `builtin`
- Score: `1.0`
- Evidence:
  - `modes/sdd/packs/builtin/openapi.yaml` via `openapi_file` (OpenAPI YAML contract found.)

### ops
- Title: Operations / Deployment
- Lifecycle: `builtin`
- Score: `1.0`
- Evidence:
  - `.github/workflows/ci.yml` via `github_actions` (GitHub Actions workflow found.)

### python
- Title: Python
- Lifecycle: `builtin`
- Score: `1.0`
- Evidence:
  - `requirements.txt` via `requirements` (Python requirements.txt found.)
  - `*.py` via `python_source` (Python source files found.)

### ui
- Title: User Interface
- Lifecycle: `builtin`
- Score: `1.0`
- Evidence:
  - `desktop/widgets/admin_chat_section.py` via `desktop_ui` (Desktop UI widget files found.)
