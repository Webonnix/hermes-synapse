# Hermes / Vexa frontend

React + TypeScript + Vite. Use Node from `.nvmrc` (20.19.5); the system Node on the development host may be too old.

```bash
nvm use
npm ci
npm run lint
npm test -- --maxWorkers=2
npm run build
```

The production Dockerfile uses `npm ci` and the committed lockfile. Build artifacts belong to the user running the build; do not generate a root-owned `dist` in the source tree. To build without touching an existing output directory, use `npm run build -- --outDir /tmp/hermes-preview`.

`npm run dev` proxies `/api` and WebSocket requests to `127.0.0.1:8000`. Authentication is required for protected APIs. The fetch interceptor adds credentials only to same-origin `/api/` requests, excluding `/api/auth/`.

Sections have shareable hash URLs, for example `/#tab=settings&section=config` and `/#tab=clients`. Browser Back/Forward restores sections. Large sections load on demand; navigation remains available during loading.

Deployment uses the root `../../docker-compose.yml` on the server, not the development compose file. Build only `frontend`, retain the previous image for rollback, then run `docker compose --env-file src/.env up -d --no-deps frontend`. Validate frontend HTTPS, backend readiness, and unauthenticated API rejection after deployment.
