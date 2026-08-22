# Prophet frontend (Next.js 16 / React 19).
FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS build
WORKDIR /app
ARG BACKEND_URL=http://backend:8000
ENV BACKEND_URL=${BACKEND_URL}
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim@sha256:2cf067cfed83d5ea958367df9f966191a942351a2df77d6f0193e162b5febfc0 AS runtime
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
WORKDIR /app
COPY --chown=node:node --from=build /app ./
USER node
EXPOSE 3000
# Next.js compiles the /api_proxy destination during the build. Override the
# build argument when the backend is not reachable as http://backend:8000.
CMD ["npm", "run", "start", "--", "--hostname", "0.0.0.0", "--port", "3000"]
