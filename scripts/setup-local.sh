#!/bin/bash
# ==============================================================================
# ALGO V1 - Local Development Startup Script
# ==============================================================================

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  ALGO V1 - Local Development Setup${NC}"
echo -e "${GREEN}========================================${NC}"

# Check Python version
python_version=$(python --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
echo -e "Python version: ${python_version}"

# Create virtual environment if not exists
if [ ! -d ".venv" ]; then
    echo -e "\n${YELLOW}Creating virtual environment...${NC}"
    python -m venv .venv
fi

# Activate virtual environment
echo -e "\n${YELLOW}Activating virtual environment...${NC}"
source .venv/bin/activate

# Install dependencies
echo -e "\n${YELLOW}Installing dependencies...${NC}"
pip install --upgrade pip
pip install -r requirements.txt

# Copy environment file if not exists
if [ ! -f ".env" ]; then
    echo -e "\n${YELLOW}Creating .env file from example...${NC}"
    cp .env.example .env
    echo -e "${RED}Please update .env with your credentials!${NC}"
fi

# Run database migrations (if using docker-compose)
echo -e "\n${GREEN}========================================${NC}"
echo -e "${GREEN}  Setup Complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "\nNext steps:"
echo -e "1. Update .env with your API keys and database credentials"
echo -e "2. Start database: docker-compose -f docker-compose.dev.yml up -d postgres redis"
echo -e "3. Apply migrations: psql -f database/001_core_schema.sql"
echo -e "4. Run the API: uvicorn src.api.main_v2:app --reload"
echo -e "\nOr run everything with Docker:"
echo -e "  docker-compose -f docker-compose.dev.yml up"
