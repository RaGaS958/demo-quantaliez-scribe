@echo off
echo Starting LiveKit Ambient Agent...
call .venv\Scripts\activate
python -m voice_agent.ambient_scribe console
pause
