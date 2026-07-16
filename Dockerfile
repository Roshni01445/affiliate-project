# Use the official Python/Playwright Linux image
FROM mcr.microsoft.com/playwright/python:v1.40.0-jammy

# Set the working directory
WORKDIR /app

# Copy requirements and install them
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install chromium

# Copy all your bot files and your chrome profile
COPY . .

ENV PYTHONUNBUFFERED=1

# Expose the Hugging Face port
EXPOSE 7860

# Start both the Telegram bot and the Flask server at the same time
CMD bash -lc "python telegram_bot.py & exec python gemini_robot.py"