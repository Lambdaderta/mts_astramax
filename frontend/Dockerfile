FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
ARG VITE_API_BASE_URL=/api/v1
ARG VITE_ENABLE_REVIEW=false
ENV VITE_DATA_SOURCE=api VITE_ENABLE_REVIEW=${VITE_ENABLE_REVIEW}
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
RUN npm run build:api

FROM nginx:stable-alpine AS runtime
ENV API_UPSTREAM=http://api:8000
ENV ENABLE_REVIEW=false REVIEW_TOKEN=""
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY --from=build /app/dist-api /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD wget -q -O /dev/null http://127.0.0.1/health || exit 1
