# X-Reach Docker Build & Cross-System Deployment Guide

This document explains how to build, test, and run the containerized X-Reach system (API, Web, Radar Daemon, and Full Compose Stack) both on external systems with Docker support and within CI/CD pipelines.

---

## 1. System Environment & Execution Limitations

### Why Docker Cannot Run Directly on This Sandbox System
- **Unprivileged Container Sandbox**: The current agent environment runs in an unprivileged Linux container sandbox (`uid=1004 (recoding)`) without `root` or `sudo` privileges.
- **No Docker Daemon (`dockerd`)**: There is no active Docker daemon socket (`/var/run/docker.sock`), and container runtimes (`docker`, `podman`, `nerdctl`, `buildah`) are not installed.
- **Nested Container Constraints**: Nesting Docker or Podman within an unprivileged sandbox container requires host kernel cgroups v2 namespace delegation and subuid/subgid mapping, which are restricted in sandbox environments.

### Ways Around It
1. **External System / Local Workstation (Recommended)**:
   - Clone or pull the repository on any computer with Docker Desktop or Docker Engine installed (macOS, Linux, or Windows WSL2).
   - Run the standard `docker build` and `docker compose` commands listed in [Section 3](#3-docker-build--run-commands).
2. **GitHub Actions CI (Automated Remote Runner)**:
   - The remote GitHub repository (`https://github.com/eeyung1/X-Reach`) runs GitHub Actions on `ubuntu-latest` with Docker, Rust, Node.js, and Python pre-installed.
   - Pushing any branch (`main`, `agent/**`, `feature/**`) automatically validates builds and test suites across the stack.
3. **Remote Docker Host (`DOCKER_HOST`)**:
   - If an external Linux VM or workstation has Docker installed and exposes its socket securely over SSH:
     ```bash
     export DOCKER_HOST="ssh://user@your-external-ip"
     docker ps
     ```
   - This allows running Docker CLI commands on this machine while executing containers remotely on the host machine.

---

## 2. Container Architecture Overview

```
                      +-----------------------------+
                      |         Web Client          |
                      | (Next.js 14 Standalone UI)  |
                      |        Port: 3000           |
                      +--------------+--------------+
                                     |
                                     | REST / SSE Stream
                                     v
+------------------------------------+------------------------------------+
|                             API Service                                 |
|                         (FastAPI + Uvicorn)                             |
|                             Port: 8000                                  |
|                                                                         |
|  +------------------------+  +-------------------+  +----------------+  |
|  | xreach-core (Rust Lib) |  | OpenCLIP ViT-B/32 |  | Radar Stream   |  |
|  | Phoenix Value Scorer   |  | 6-Level RVQ SID   |  | Ring Buffer    |  |
|  +------------------------+  +-------------------+  +----------------+  |
+------------------------------------+------------------------------------+
                                     |
            +------------------------+------------------------+
            | Shared Volume: /models | Shared Volume: /data   |
            | Model weights & cache  | Radar captures & JSON  |
            +------------------------+------------------------+
```

- **`docker/Dockerfile.api`**:
  - **Multi-Stage Build**:
    1. `rust-build` (`rust:1-bookworm`): Compiles the Rust scoring bridge `xreach-api-scorer` from `services/api/rust-bridge` and `crates/xreach-core`.
    2. `python-build` (`python:3.11-slim-bookworm`): Installs `services/multimodal[inference]`, `services/radar`, and `services/api`.
    3. `runtime` (`python:3.11-slim-bookworm`): Unprivileged `xreach` user (`uid=10001`), shared volumes `/models` and `/data`, healthcheck `GET /healthz`.
- **`docker/Dockerfile.web`**:
  - Multi-stage Next.js standalone container (`node:22-alpine`) serving static and server-rendered assets with unprivileged `nextjs` user (`uid=10001`).
- **`docker-compose.yml`**:
  - Orchestrates API, Web, Radar worker, and persistent volume caching.

---

## 3. Docker Build & Run Commands

Run all commands from the root of the repository (`/path/to/X-Reach`):

### A. API Service (`xreach-api`)

```bash
# 1. Build the API container image
docker build -f docker/Dockerfile.api -t x-reach-api:latest .

# 2. Create local data directory for persistent captures / calibration
mkdir -p data

# 3. Run the API container
docker run -d \
  --name x-reach-api \
  -p 8000:8000 \
  --mount type=volume,source=xreach-model-cache,target=/models \
  --mount type=bind,source="$(pwd)/data",target=/data \
  -e XREACH_RADAR_CAPTURE_FILE=/data/radar-capture.json \
  x-reach-api:latest

# 4. Verify service health
curl -f http://localhost:8000/healthz

# 5. Check API documentation and endpoints
curl http://localhost:8000/docs
```

### B. Web Dashboard (`xreach-web`)

```bash
# 1. Build the Web container image
docker build -f docker/Dockerfile.web -t x-reach-web:latest .

# 2. Run the Web container
docker run -d \
  --name x-reach-web \
  -p 3000:3000 \
  -e NEXT_PUBLIC_API_URL=http://localhost:8000 \
  x-reach-web:latest

# 3. Verify web frontend
curl -f http://localhost:3000/
# Open http://localhost:3000 in your browser
```

### C. Full Stack Orchestration (`docker compose`)

```bash
# 1. Launch all services with hot builds and volume attachments
docker compose up --build -d

# Or use the convenience helper script:
./scripts/start.sh up

# 2. View streaming logs across services
docker compose logs -f
# Or: ./scripts/start.sh logs

# 3. Gateway verification (Nginx on port 8080)
curl -f http://localhost:8080/healthz
curl -f http://localhost:8080/

# 4. Run end-to-end smoke tests against the compose stack
./scripts/e2e.sh

# 5. Stop services
docker compose down
# Or: ./scripts/start.sh down
```

---

## 4. Cross-System Checklist (Working on Another Machine)

When cloning or pulling onto another computer:

- [x] **Install Prerequisites**:
  - [Docker Engine](https://docs.docker.com/engine/install/) (>= 24.0) or [Docker Desktop](https://www.docker.com/products/docker-desktop/)
  - [Docker Compose](https://docs.docker.com/compose/) (>= 2.20)
  - Git
- [x] **Clone the Repository**:
  ```bash
  git clone https://github.com/eeyung1/X-Reach.git
  cd X-Reach
  git checkout main
  ```
- [x] **Build and Smoke-Test the API**:
  *(CI Verified in GitHub Actions run [#41705797810](https://github.com/eeyung1/X-Reach/actions/runs/41705797810))*
  ```bash
  docker build -f docker/Dockerfile.api -t x-reach-api .
  docker run --rm -p 8000:8000 x-reach-api
  curl http://localhost:8000/healthz
  ```
- [x] **Test Post Analysis Endpoint**:
  *(CI Verified in GitHub Actions run [#41705797810](https://github.com/eeyung1/X-Reach/actions/runs/41705797810))*
  ```bash
  curl -X POST http://localhost:8000/api/v1/analyze \
    -H "Content-Type: application/json" \
    -d '{
      "text": "Deep dive into viral distribution mechanics on X using the Phoenix algorithm.",
      "author_id": 123456789,
      "candidate_id": 987654321
    }'
  ```
- [x] **Test Live Radar Opportunities Stream**:
  *(CI Verified in GitHub Actions run [#41705797810](https://github.com/eeyung1/X-Reach/actions/runs/41705797810))*
  ```bash
  curl -N http://localhost:8000/api/v1/radar/stream
  ```
- [x] **Build and Smoke-Test Web Frontend**:
  *(CI Verified in GitHub Actions run [#41705797810](https://github.com/eeyung1/X-Reach/actions/runs/41705797810))*
  ```bash
  docker build -f docker/Dockerfile.web -t x-reach-web .
  docker run --rm -p 3000:3000 x-reach-web
  ```
- [x] **End-to-End Compose Stack Verification**:
  *(CI Verified in GitHub Actions run [#41705797810](https://github.com/eeyung1/X-Reach/actions/runs/41705797810))*
  ```bash
  chmod 777 data && ./scripts/start.sh up
  curl -f http://127.0.0.1:8080/healthz
  pytest tests/e2e/ -v
  ./scripts/start.sh down
  ```

---

## 5. Development Notes

- **Model Cache Persistence**: The `/models` volume caches OpenCLIP weights (`hf-hub:laion/CLIP-ViT-B-32-laion2B-s34B-b79K`). Persisting this volume prevents redownloading (~600MB) on container restart.
- **Unprivileged Runtime UID**: Both containers execute under non-root users (`uid=10001`). When bind-mounting host directories to `/data`, ensure permissions allow read/write by UID 10001 (`chmod 777 data` or `chown 10001:10001 data`).

