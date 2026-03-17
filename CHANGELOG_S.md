# Deletions : 
1. .git file - to reinitiate a repo
2. .pytest_cache - cache files from pytest lib 
3. .venv - virtual env for python development , no need to store it
4. .vscode - specific for each dev env
5. Elite-Assets/node_modules - self generated staff should be deleted
6. frontend/.next && frontend/node_modules - self generated staff should be deleted
7. monitoring/** - no need for monitoring for current setup
8. nginx/** - new will be written, for new system architecture
9. pgadmin - no need for database visualisation



# Notes on directories:
1. .vite - #TODO : need to check connections with this folder, guessing something related to the front
2. alembic - migrations folder, stores history of database migrations, #TODO: need to check with which service of the project it is related
3. database - handbook of lot of sql commands that create business logic or prepare to it
4. Elite-Assets - main frontend, landing/info page about product, built in React + Vite.js
5. frontend - main lms system built in NextJS
6. scripts - some scripts that hold entrypoint, mostly related to the backend
7. src - backend source itself
8. tests - backend tests 



# Services to be separated: 
1. Elite-Assets - will be an absolutely separate front end landing web site
2. frontend - Algo main LMS system Front-End
3. backend - Algo main LMS system Back-End 
