def main() -> None:
    import uvicorn

    uvicorn.run("the_bot_radio.web.main:app", host="127.0.0.1", port=8000, reload=True)
