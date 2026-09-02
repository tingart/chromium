FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy and install Python dependencies
COPY ./requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --upgrade -r /app/requirements.txt

# Install Playwright and its system dependencies
RUN playwright install --with-deps chromium

# Copy the rest of the application code
COPY . /app

# Create storage directory for persistent login data
RUN mkdir -p /app/user_data

# Expose the application port
EXPOSE 7860

# Run the application
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
