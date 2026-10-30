# Stage 1: Build Vite React application
FROM node:20-alpine@sha256:fb4cd12c85ee03686f6af5362a0b0d56d50c58a04632e6c0fb8363f609372293 AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm ci

COPY . .

# Build identity for the service worker's cache key. The image build has no git
# checkout, so the deploy passes it in; without it the plugin falls back to a
# build timestamp, which still changes per build.
ARG HERMES_BUILD_ID=""
ENV HERMES_BUILD_ID=$HERMES_BUILD_ID
RUN npm run build

# Stage 2: Serve application using Nginx
FROM nginx:alpine@sha256:4a73073bd557c65b759505da037898b61f1be6cbcc3c2c3aeac22d2a470c1752

# Copy built assets from build stage to nginx default public directory
COPY --from=build /app/dist /usr/share/nginx/html

# Copy nginx proxy config as a template — the official nginx:alpine
# entrypoint envsubst's *.template files under /etc/nginx/templates/ into
# /etc/nginx/conf.d/ (stripping the .template suffix) at container start,
# substituting only variables that are actually set in the container's
# environment. This is how LLM_PROXY_TOKEN gets baked into the config.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY security-headers.conf /etc/nginx/snippets/hermes-security.conf
COPY security-common.conf /etc/nginx/snippets/hermes-security-common.conf

# Expose Nginx port
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
