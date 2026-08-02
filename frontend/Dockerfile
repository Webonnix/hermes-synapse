# Stage 1: Build Vite React application
FROM node:20-alpine AS build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
RUN npm run build

# Stage 2: Serve application using Nginx
FROM nginx:alpine

# Copy built assets from build stage to nginx default public directory
COPY --from=build /app/dist /usr/share/nginx/html

# Copy nginx proxy config as a template — the official nginx:alpine
# entrypoint envsubst's *.template files under /etc/nginx/templates/ into
# /etc/nginx/conf.d/ (stripping the .template suffix) at container start,
# substituting only variables that are actually set in the container's
# environment. This is how OLLAMA_PROXY_TOKEN gets baked into the config.
COPY nginx.conf.template /etc/nginx/templates/default.conf.template

# Expose Nginx port
EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
