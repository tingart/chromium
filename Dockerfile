FROM lscr.io/linuxserver/chromium:latest

# Render Environment Port Setup
ENV PORT=7860
ENV CUSTOM_PORT=7860
EXPOSE 7860

# Disable internal HTTPS so Render's HTTP proxy works
ENV KASM_PORT=7860
ENV SSL_CERT_FILE=""

# Mobile Touch & Performance Optimization Flags for 0.1 vCPU
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --touch-events=enabled --disable-gpu"

# Auto Scale & Resolution configuration
ENV DISPLAY_WIDTH=1024
ENV DISPLAY_HEIGHT=600
