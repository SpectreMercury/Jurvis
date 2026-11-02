import logging
import uvicorn
from .app import create_app
from .config import Settings

def main():
    settings=Settings();settings.prepare()
    logging.basicConfig(filename=settings.data/"logs/engine.log",level=logging.INFO,format="%(asctime)s %(levelname)s %(name)s %(message)s")
    app=create_app(settings)
    security=app.state.security
    uvicorn.run(app,host=settings.host,port=settings.port,ssl_certfile=str(security.cert),ssl_keyfile=str(security.key),access_log=False)

if __name__=="__main__":main()
