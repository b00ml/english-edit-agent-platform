# frontend/Dockerfile —— 多阶段构建前端
# 阶段 1：使用 Node 构建静态资源
FROM node:26-alpine AS build

WORKDIR /app

# 先复制依赖清单，充分利用构建缓存
COPY package.json ./
COPY package-lock.json* ./

# 安装依赖并构建
RUN npm ci
COPY . .
RUN npm run build

# 阶段 2：使用 nginx 提供静态资源并反向代理 /api
FROM nginx:alpine

# 复制构建产物与 nginx 配置
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]
