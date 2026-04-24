@echo off
echo [단순경비율 계산기 실행]
echo.
echo ANTHROPIC_API_KEY 환경변수를 설정해주세요.
echo 예) set ANTHROPIC_API_KEY=sk-ant-...
echo.
if "%ANTHROPIC_API_KEY%"=="" (
    set /p ANTHROPIC_API_KEY=API 키 입력:
)
echo 브라우저에서 http://localhost:8501 접속하세요
streamlit run "%~dp0app.py"
pause
