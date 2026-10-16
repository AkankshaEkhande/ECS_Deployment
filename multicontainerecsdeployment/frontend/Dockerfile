FROM nginx:1.27-alpine

# Official nginx image renders /etc/nginx/templates/*.template with env vars at startup
COPY nginx.conf.template /etc/nginx/templates/default.conf.template
COPY index.html login.html /usr/share/nginx/html/

# compose: "backend" (service name). ECS (same task): 127.0.0.1
ENV BACKEND_HOST=backend

EXPOSE 80
