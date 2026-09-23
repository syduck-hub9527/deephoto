"""手动启动入口:python -m deephoto(由用户执行,不在自动化流程中启动服务)。"""

from __future__ import annotations


def main() -> None:
    import uvicorn

    uvicorn.run("deephoto.api.app:create_app", factory=True,
                host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
