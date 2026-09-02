FROM lscr.io/linuxserver/chromium:latest

# Render Environment Port Setup
ENV PORT=7860
ENV CUSTOM_PORT=7860
EXPOSE 7860

# Internal Nginx ko Plain HTTP (NO_TLS) par force karne ke liye
ENV NO_TLS=1
ENV SUBFOLDER=/

# Mobile Touch & 0.1 vCPU Performance Flags
ENV CHROMIUM_FLAGS="--no-sandbox --disable-dev-shm-usage --touch-events=enabled --disable-gpu"

# Screen Resolution Configuration
ENV DISPLAY_WIDTH=1024
ENV DISPLAY_HEIGHT=600
