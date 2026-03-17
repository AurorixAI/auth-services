# ALGO V1 - Local Development Startup Script for Windows
# ==============================================================================

Write-Host "========================================"
Write-Host "  ALGO V1 - Local Development Setup"
Write-Host "========================================" -ForegroundColor Green

# Check Python version
$pythonVersion = python --version 2>&1
Write-Host "Python version: $pythonVersion"

# Create virtual environment if not exists
if (-not (Test-Path ".venv")) {
    Write-Host "`nCreating virtual environment..." -ForegroundColor Yellow
    python -m venv .venv
}

# Activate virtual environment
Write-Host "`nActivating virtual environment..." -ForegroundColor Yellow
.\.venv\Scripts\Activate.ps1

# Install dependencies
Write-Host "`nInstalling dependencies..." -ForegroundColor Yellow
pip install --upgrade pip
pip install -r requirements.txt

# Copy environment file if not exists
if (-not (Test-Path ".env")) {
    Write-Host "`nCreating .env file from example..." -ForegroundColor Yellow
    Copy-Item .env.example .env
    Write-Host "Please update .env with your credentials!" -ForegroundColor Red
}

Write-Host "`n========================================"
Write-Host "  Setup Complete!"
Write-Host "========================================" -ForegroundColor Green

Write-Host "`nNext steps:"
Write-Host "1. Update .env with your API keys and database credentials"
Write-Host "2. Start database: docker-compose -f docker-compose.dev.yml up -d postgres redis"
Write-Host "3. Apply migrations in order:"
Write-Host "   - database/001_core_schema.sql"
Write-Host "   - database/002_presentation_schema.sql"
Write-Host "   - database/003_analytics_schema.sql"
Write-Host "   - database/004_seed_data.sql"
Write-Host "4. Run the API: uvicorn src.api.main_v2:app --reload"
Write-Host "`nOr run everything with Docker:"
Write-Host "  docker-compose -f docker-compose.dev.yml up"
