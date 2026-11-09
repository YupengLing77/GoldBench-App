"""Start the local API with optional ignored runtime credentials."""
from . import store
from .trial import load_credentials

if __name__ == '__main__':
    import uvicorn
    load_credentials(store.ROOT/'runtime/qwen.env')
    uvicorn.run('goldbench.api:app',host='127.0.0.1',port=8765)
