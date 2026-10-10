# Prophet frontend (Next.js 16 / React 19).
FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS build
WORKDIR /app
ARG BACKEND_URL=http://backend:8000
ENV BACKEND_URL=${BACKEND_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:24-slim@sha256:ba849c60be29959425b8734d57b8b4b7d56f98edd9504c9af091d5281095a71e AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --chown=node:node --from=build /app ./
USER node
EXPOSE 3000
# Next.js compiles the /api_proxy destination during the build. Override the
# build argument when the backend is not reachable as http://backend:8000.
CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
