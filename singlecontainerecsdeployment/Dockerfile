# ---- Base image ----
FROM python:3.12-slim

# Python behaviour: no .pyc files, unbuffered logs (so CloudWatch gets logs instantly)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (Docker layer caching: only re-runs if requirements change)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py .

# Run as non-root user (security best practice)
RUN useradd --create-home appuser
USER appuser

# Document the port the app listens on
EXPOSE 5000

# Production server (not Flask's dev server)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "2", "app:app"]
