"""Administrator local dataset browser; auth enforced by shared route policy."""
from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse


def data_query_router(templates):
    router = APIRouter()

    @router.get('/market-collection/query')
    def page(request: Request):
        from core.realtime.trading_session import shanghai_now
        return templates.TemplateResponse(request, 'data_query.html', {'today': shanghai_now().strftime('%Y-%m-%d')})

    @router.get('/api/admin/market-collection/datasets')
    def datasets():
        from core.operations.data_browser import catalog
        return {'ok': True, 'datasets': catalog()}

    @router.get('/api/admin/market-collection/query')
    def rows(layer: str, dataset: str, start: str, end: str, code: str = '',
             name: str = '', source: str = '', page: int = Query(1, ge=1, le=10000),
             page_size: int = Query(50, ge=1, le=200), sort: str = '', descending: bool = False):
        from config.settings import FACTOR_DB_PATH, CACHE_DIR
        from core.operations.data_browser import query
        try:
            return {'ok': True, 'data': query(db_path=FACTOR_DB_PATH, cache_dir=CACHE_DIR,
                layer=layer, dataset=dataset, start=start, end=end, code=code, name=name,
                source=source, page=page, page_size=page_size, sort=sort, descending=descending)}
        except ValueError as exc:
            return JSONResponse({'ok': False, 'message': str(exc)}, status_code=400)
        except Exception:
            from loguru import logger
            logger.exception("底层数据查询失败")
            return JSONResponse({'ok': False, 'message': '数据暂不可读，可能正在写入，请稍后重试并检查服务日志'}, status_code=409)
    return router
