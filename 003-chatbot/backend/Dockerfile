# LLASTA Chatbot Backend - Dockerfile
#
# Ce Dockerfile crée une image légère pour le backend FastAPI
# Basé sur Python 3.11 slim pour optimiser la taille

FROM python:3.11-slim

# Métadonnées de l'image
LABEL maintainer="LLASTA Project"
LABEL description="Backend FastAPI pour le chatbot LLASTA"
LABEL version="1.0.0"

# Variables d'environnement pour Python
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Créer un utilisateur non-root pour la sécurité
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Répertoire de travail
WORKDIR /app

# Copier les dépendances et les installer
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copier le code source
COPY main.py .

# Créer le répertoire frontend et copier les fichiers statiques
COPY ../frontend ./frontend

# Changer la propriété des fichiers
RUN chown -R appuser:appuser /app

# Passer à l'utilisateur non-root
USER appuser

# Exposer le port
EXPOSE 8080

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Commande de démarrage
CMD ["python", "main.py"]
