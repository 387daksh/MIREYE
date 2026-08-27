FROM node:24-alpine AS build
WORKDIR /app
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_BASEMAP_STYLE_URL=
ARG NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL=
ENV NEXT_PUBLIC_BASEMAP_STYLE_URL=$NEXT_PUBLIC_BASEMAP_STYLE_URL
ENV NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL=$NEXT_PUBLIC_BASEMAP_FALLBACK_STYLE_URL
COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts
COPY . .
RUN npm run build

FROM node:24-alpine
WORKDIR /app
ENV NODE_ENV=production HOSTNAME=0.0.0.0
COPY --from=build /app/.next/standalone ./
COPY --from=build /app/.next/static ./.next/static
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 CMD wget -q -O - http://127.0.0.1:3000/ >/dev/null
CMD ["node", "server.js"]
