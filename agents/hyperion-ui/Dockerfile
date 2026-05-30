# --- build stage -----------------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# VITE_HYPERION_API is baked in at build time (Vite inlines import.meta.env).
# Browser → host, so default to the published host port, not the service name.
ARG VITE_HYPERION_API=http://localhost:4100
ENV VITE_HYPERION_API=$VITE_HYPERION_API

COPY package.json package-lock.json* ./
RUN npm install
COPY . .
RUN npm run build

# --- serve stage -----------------------------------------------------------
FROM nginx:alpine
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 4102
CMD ["nginx", "-g", "daemon off;"]
