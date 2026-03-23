# Usa la imagen oficial de uv para mayor velocidad
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim

# Establecer el directorio de trabajo
WORKDIR /app

# Evitar que Python genere archivos .pyc y habilitar el buffer de salida para ver logs en tiempo real
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# Instalar las dependencias primero (aprovechando el cache de capas de Docker)
# Copiamos pyproject.toml y uv.lock (si existe)
COPY pyproject.toml uv.lock* ./

# Instalamos las dependencias sin instalar el proyecto todavía
RUN uv sync --frozen --no-install-project

# Copiamos el resto del código y el archivo JSON necesario
COPY src/ ./src/ 

# Instalamos el proyecto
RUN uv sync --frozen

# Ejecutamos el bot
# Usamos 'uv run' para asegurarnos de que se use el entorno virtual creado
CMD ["uv", "run", "fcp-bot"]