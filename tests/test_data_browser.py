from pathlib import Path
import duckdb
import pytest
from core.operations.data_browser import query


def test_silver_filter_pagination_sort_and_chinese_labels(tmp_path):
    db=tmp_path/'test.duckdb'
    with duckdb.connect(str(db)) as con:
        con.execute('CREATE TABLE stock_daily_silver(trade_date VARCHAR, code VARCHAR, name VARCHAR, close DOUBLE, source VARCHAR)')
        con.execute("INSERT INTO stock_daily_silver VALUES ('20261029','000001','银行',10,'tushare'),('20261029','000002','银行二',11,'tushare'),('20261028','000001','银行',9,'tushare')")
    args=dict(db_path=db,cache_dir=tmp_path,layer='silver',dataset='stock_daily_silver',start='20261029',end='20261029',name='银行',page_size=1,sort='close',descending=True)
    result=query(**args)
    assert result['total']==2 and result['rows'][0]['code']=='000002'
    assert query(**args,page=2)['rows'][0]['code']=='000001'
    assert {'key':'close','label':'收盘价'} in result['columns']
    assert query(**{**args,'name':"' OR 1=1 --"})['total']==0
    with pytest.raises(ValueError): query(**{**args,'sort':'close; DROP TABLE stock_daily_silver'})
    with pytest.raises(ValueError): query(**{**args,'dataset':'../secrets'})


def test_raw_preserves_zero_prefix_and_missing_file(tmp_path):
    args=dict(db_path=tmp_path/'missing',cache_dir=tmp_path,layer='raw',dataset='limit_up',start='20261029',end='20261029')
    assert query(**args)['total']==0
    folder=tmp_path/'market/limit_up';folder.mkdir(parents=True)
    (folder/'20261029.csv').write_text('ts_code,name,amount\n000001.SZ,银行,100\n')
    result=query(**args,code='000001')
    assert result['total']==1 and result['rows'][0]['ts_code']=='000001.SZ'
    with pytest.raises(ValueError):query(**{**args,'end':'20261030'})


def test_query_routes_keep_admin_boundary():
    from web.permissions import requires_admin, permission_key_for_path
    for path in ('/market-collection/query','/api/admin/market-collection/query','/api/admin/market-collection/datasets'):
        assert requires_admin(path)
        assert permission_key_for_path(path)=='fetch'


def test_http_validation_and_query(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from web.data_query_router import data_query_router
    from config import settings
    monkeypatch.setattr(settings, 'FACTOR_DB_PATH', tmp_path/'none.duckdb')
    monkeypatch.setattr(settings, 'CACHE_DIR', tmp_path)
    app=FastAPI();app.include_router(data_query_router(None))
    client=TestClient(app)
    assert client.get('/api/admin/market-collection/datasets').json()['datasets']
    params=dict(layer='silver',dataset='sector_daily_silver',start='20261029',end='20261029')
    response=client.get('/api/admin/market-collection/query',params=params)
    assert response.status_code==200 and response.json()['data']['total']==0
    assert client.get('/api/admin/market-collection/query',params={**params,'page_size':1000}).status_code==422
    assert client.get('/api/admin/market-collection/query',params={**params,'dataset':'secrets'}).status_code==400
