#coding=utf-8
from gevent import monkey
monkey.patch_all()
import os
import time
import json
import copy
import datetime
import traceback
import const as ct
import numpy as np
import pandas as pd
from log import getLogger
from cstock import CStock
from cindex import CIndex
from climit import CLimit 
from functools import partial
from datetime import datetime
from rstock import RIndexStock
from ccalendar import CCalendar
from animation import CAnimation
from index_info import IndexInfo
from ticks import download, unzip
from cstock_info import CStockInfo
from combination import Combination
from industry_info import IndustryInfo
from datamanager.margin  import Margin
from datamanager.emotion import Emotion
from datamanager.bull_stock_ratio import BullStockRatio
from datamanager.hgt import StockConnect
from datamanager.sexchange import StockExchange
from rindustry import RIndexIndustryInfo
from combination_info import CombinationInfo
from futuquant.common.constant import SubType
from subscriber import Subscriber, StockQuoteHandler, TickerHandler
from common import is_trading_time, delta_days, create_redis_obj, add_prifix, add_index_prefix, kill_process, concurrent_run, get_day_nday_ago, get_dates_array, process_concurrent_run
pd.options.mode.chained_assignment = None #default='warn'
pd.set_option('display.max_columns', None)
pd.set_option('display.max_rows', None)
class DataManager:
    def __init__(self, dbinfo = ct.DB_INFO, redis_host = None):
        self.dbinfo = dbinfo
        self.logger = getLogger(__name__)
        self.index_objs = dict()
        self.stock_objs = dict()
        self.combination_objs = dict()
        self.cal_client = CCalendar(dbinfo, redis_host)
        self.index_info_client = IndexInfo()
        self.comb_info_client = CombinationInfo(dbinfo, redis_host)
        self.stock_info_client = CStockInfo(dbinfo, redis_host)
        self.rindex_stock_data_client = RIndexStock(dbinfo, redis_host) 
        self.industry_info_client = IndustryInfo(dbinfo, redis_host)
        self.rindustry_info_client = RIndexIndustryInfo(dbinfo, redis_host)
        self.limit_client = CLimit(dbinfo, redis_host)
        self.animation_client = CAnimation(dbinfo, redis_host)
        self.subscriber = Subscriber()
        self.quote_handler  = StockQuoteHandler()
        self.ticker_handler = TickerHandler()
        self.connect_client = StockConnect(market_from = ct.SH_MARKET_SYMBOL, market_to = ct.HK_MARKET_SYMBOL, dbinfo = dbinfo, redis_host = redis_host)
        self.margin_client = Margin(dbinfo = dbinfo, redis_host = redis_host) 
        self.emotion_client = Emotion(dbinfo = dbinfo, redis_host = redis_host)
        self.sh_exchange_client = StockExchange(ct.SH_MARKET_SYMBOL)
        self.sz_exchange_client = StockExchange(ct.SZ_MARKET_SYMBOL)

    def is_collecting_time(self, now_time = datetime.now()):
        _date = now_time.strftime('%Y-%m-%d')
        y,m,d = time.strptime(_date, "%Y-%m-%d")[0:3]
        aft_open_hour,aft_open_minute,aft_open_second = (19,00,00)
        aft_open_time = datetime(y,m,d,aft_open_hour,aft_open_minute,aft_open_second)
        aft_close_hour,aft_close_minute,aft_close_second = (23,59,59)
        aft_close_time = datetime(y,m,d,aft_close_hour,aft_close_minute,aft_close_second)
        return aft_open_time < now_time < aft_close_time

    def is_morning_time(self, now_time = datetime.now()):
        _date = now_time.strftime('%Y-%m-%d')
        y,m,d = time.strptime(_date, "%Y-%m-%d")[0:3]
        mor_open_hour,mor_open_minute,mor_open_second = (0,0,0)
        mor_open_time = datetime(y,m,d,mor_open_hour,mor_open_minute,mor_open_second)
        mor_close_hour,mor_close_minute,mor_close_second = (6,30,0)
        mor_close_time = datetime(y,m,d,mor_close_hour,mor_close_minute,mor_close_second)
        return mor_open_time < now_time < mor_close_time

    def collect_combination_runtime_data(self):
        def _combination_run(code_id):
            self.combination_objs[code_id].run()
            return (code_id, True)
        todo_iplist = list(self.combination_objs.keys())
        return concurrent_run(_combination_run, todo_iplist, num = 10)

    def collect_stock_runtime_data(self):
        if self.ticker_handler.empty(): return
        datas = self.ticker_handler.getQueue()
        while not datas.empty():
            df = datas.get()
            df = df.set_index('time')
            df.index = pd.to_datetime(df.index)
            for code_str in set(df.code):
                code_id = code_str.split('.')[1]
                self.stock_objs[code_id].run(df.loc[df.code == code_str])

    def init_real_stock_info(self):
        concerned_list = self.comb_info_client.get_concerned_list()
        prefix_concerned_list = [add_prifix(code) for code in concerned_list]
        ret = self.subscriber.subscribe(prefix_concerned_list, SubType.TICKER, self.ticker_handler)
        if 0 == ret:
            for code in concerned_list:
                if code not in self.stock_objs:
                    self.stock_objs[code] = CStock(code, self.dbinfo, should_create_influxdb = True, should_create_mysqldb = True)
        return ret

    def init_index_info(self):
        index_list = ct.INDEX_DICT.keys()
        prefix_index_list = [add_index_prefix(code) for code in index_list]
        ret = self.subscriber.subscribe(prefix_index_list, SubType.QUOTE, self.quote_handler)
        if 0 != ret:
            self.logger.error("subscribe for index list failed")
            return ret
        for code in index_list: 
            if code not in self.index_objs:
                self.index_objs[code] = CIndex(code, should_create_influxdb = True, should_create_mysqldb = True)

    def collect_index_runtime_data(self):
        if self.quote_handler.empty(): return
        datas = self.quote_handler.getQueue()
        while not datas.empty():
            df = datas.get()
            df['time'] = df.data_date + ' ' + df.data_time
            df = df.drop(['data_date', 'data_time'], axis = 1)
            df = df.set_index('time')
            df.index = pd.to_datetime(df.index)
            for code_str in set(df.code):
                code_id = code_str.split('.')[1]
                self.index_objs[code_id].run(df.loc[df.code == code_str])

    def run(self, sleep_time):
        while True:
            try:
                if self.cal_client.is_trading_day():
                    if is_trading_time():
                        sleep_time = 1
                        if not self.subscriber.status():
                            self.subscriber.start()
                            if 0 == self.init_index_info() and 0 == self.init_real_stock_info():
                                self.init_combination_info()
                            else:
                                self.logger.debug("enter stop dict time")
                                self.subscriber.stop()
                        else:
                            self.collect_stock_runtime_data()
                            self.collect_combination_runtime_data()
                            self.collect_index_runtime_data()
                            self.animation_client.collect()
                    else:
                        sleep_time = 60
                        if self.subscriber.status():
                            self.subscriber.stop()
            except Exception as e:
                traceback.print_exc()
                self.logger.error(e)
            time.sleep(sleep_time)

    def set_update_info(self, step_length, exec_date, cdate = None, filename = ct.STEPFILE):
        step_info = dict()
        if cdate is None: cdate = 'none'
        step_info[cdate] = dict()
        step_info[cdate]['step'] = step_length
        step_info[cdate]['date'] = exec_date
        with open(filename, 'w') as f:
            json.dump(step_info, f)
        self.logger.info("finish step :%s" %  step_length)

    def get_update_info(self, cdate = None, exec_date = None, filename = ct.STEPFILE):
        if cdate is None: cdate = 'none'
        if not os.path.exists(filename): return (0, exec_date)
        with open(filename, 'r') as f: step_info = json.load(f)
        if cdate not in step_info: return (0, exec_date)
        return (step_info[cdate]['step'], step_info[cdate]['date'])

    def bootstrap(self, cdate = None, exec_date = datetime.now().strftime('%Y-%m-%d')):
        finished_step, exec_date = self.get_update_info(cdate, exec_date)
        self.logger.info("enter updating.%s" % finished_step)
        if finished_step < 1:
            if not self.cal_client.init():
                self.logger.error("cal client init failed")
                return False
            self.set_update_info(1, exec_date, cdate)

        if finished_step < 2:
            if not self.index_info_client.update():
                self.logger.error("index info init failed")
                return False
            self.set_update_info(2, exec_date, cdate)

        if finished_step < 3:
            if not self.stock_info_client.update():
                self.logger.error("stock info init failed")
                return False
            self.set_update_info(3, exec_date, cdate)

        if finished_step < 4:
            if not self.comb_info_client.update():
                self.logger.error("comb info init failed")
                return False
            self.set_update_info(4, exec_date, cdate)

        if finished_step < 5:
            if not self.industry_info_client.update():
                self.logger.error("industry info init failed")
                return False
            self.set_update_info(5, exec_date, cdate)

        if finished_step < 6:
            if not self.download_and_extract(exec_date):
                self.logger.error("download and extract failed")
                return False
            self.set_update_info(6, exec_date, cdate)

        if finished_step < 7:
            if not self.init_tdx_index_info(cdate):
                self.logger.error("init tdx index info failed")
                return False
            self.set_update_info(7, exec_date, cdate)

        if finished_step < 8:
            if not self.sh_exchange_client.update(exec_date, num = 30):
                self.logger.error("sh exchange update failed")
                return False
            self.set_update_info(8, exec_date, cdate)

        if finished_step < 9:
            if not self.sz_exchange_client.update(exec_date, num = 30):
                self.logger.error("sz exchange update failed")
                return False
            self.set_update_info(9, exec_date, cdate)

        if finished_step < 10:
            if not self.init_index_components_info(exec_date):
                self.logger.error("init index components info failed")
                return False
            self.set_update_info(10, exec_date, cdate)

        if finished_step < 11:
            if not self.init_industry_info(cdate):
                self.logger.error("init industry info failed")
                return False
            self.set_update_info(11, exec_date, cdate)

        if finished_step < 12:
            if not self.rindustry_info_client.update(exec_date):
                self.logger.error("init %s rindustry info failed" % exec_date)
                return False
            self.set_update_info(12, exec_date, cdate)

        if finished_step < 13:
            if not self.limit_client.update(exec_date):
                self.logger.error("init limit info failed")
                return False
            self.set_update_info(13, exec_date, cdate)

        if finished_step < 14:
            if not self.init_yesterday_hk_info(exec_date):
                self.logger.error("init yesterday hk info failed")
                return False
            self.set_update_info(14, exec_date, cdate)

        if finished_step < 15:
            if not self.margin_client.update(exec_date):
                self.logger.error("init yesterday margin failed")
                return False
            self.set_update_info(15, exec_date, cdate)

        if finished_step < 16:
            if not self.init_stock_info(cdate):
                self.logger.error("init stock info set failed")
                return False
            self.set_update_info(16, exec_date, cdate)

        if finished_step < 17:
            if not self.init_base_float_profit():
                self.logger.error("init base float profit for all stock")
                return False
            self.set_update_info(17, exec_date, cdate)

        if finished_step < 18:
            if not self.rindex_stock_data_client.update(exec_date, num = 30):
                self.logger.error("rstock data set failed")
                return False
            self.set_update_info(18, exec_date, cdate)

        if finished_step < 18:
            if not self.set_bull_stock_ratio(exec_date, num = 30):
                self.logger.error("bull ratio set failed")
                return False
            self.set_update_info(18, exec_date, cdate)
        self.logger.info("updating succeed")
        return True
        
    def update(self, sleep_time):
        while True:
            self.logger.info("enter daily update process. %s" % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
            try:
                if self.cal_client.is_trading_day(): 
                    self.logger.info("is trading day. %s" % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                    if self.is_collecting_time():
                        self.logger.info("is collecting time. %s" % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                        self.bootstrap(cdate = datetime.now().strftime('%Y-%m-%d'))
            except Exception as e:
                kill_process("google-chrome")
                kill_process("renderer")
                kill_process("Xvfb")
                kill_process("zygote")
                kill_process("defunct")
                kill_process("show-component-extension-options")
                self.logger.error(e)
            time.sleep(sleep_time)

    def init_combination_info(self):
        trading_info = self.comb_info_client.get()
        for _, code_id in trading_info['code'].iteritems():
            if str(code_id) not in self.combination_objs:
                self.combination_objs[str(code_id)] = Combination(code_id, self.dbinfo)

    def init_base_float_profit(self):
        def _set_base_float_profit(code_id):
            if CStock(code_id).set_base_floating_profit():
                self.logger.info("%s set base float profit success" % code_id)
                return (code_id, True)
            else:
                self.logger.error("%s set base float profit failed" % code_id)
                return (code_id, False)
        failed_list = self.stock_info_client.get().code.tolist()
        self.logger.info("%s stocks to be set" % len(failed_list))
        return process_concurrent_run(_set_base_float_profit, failed_list, num = 500)

    def compute_base_float_profit(self):
        def _compute_base_float_profit(code_id):
            if CStock(code_id).compute_floating_profit():
                self.logger.info("%s set base float profit success" % code_id)
                return (code_id, True)
            else:
                self.logger.error("%s set base float profit failed" % code_id)
                return (code_id, False)
        failed_list = self.stock_info_client.get().code.tolist()
        self.logger.info("%s stocks to be set" % len(failed_list))
        return process_concurrent_run(_compute_base_float_profit, failed_list, num = 500)

    def init_stock_info(self, cdate = None):
        def _set_stock_info(_date, bonus_info, index_info, code_id):
            try:
                if CStock(code_id).set_k_data(bonus_info, index_info, _date):
                    self.logger.info("%s set k data success" % code_id)
                    return (code_id, True)
                else:
                    self.logger.error("%s set k data failed" % code_id)
                    return (code_id, False)
            except Exception as e:
                self.logger.error("%s set k data exception:%s" % (code_id, e))
                return (code_id, False)

        #get stock bonus info
        bonus_info = pd.read_csv("/data/tdx/base/bonus.csv", sep = ',',
                      dtype = {'code' : str, 'market': int, 'type': int, 'money': float, 'price': float, 'count': float, 'rate': float, 'date': int})

        index_info = CIndex('000001').get_k_data()
        if index_info is None or index_info.empty: return False
        df = self.stock_info_client.get()
        failed_list = df.code.tolist()
        self.logger.info("all code list length:%s" % len(failed_list))
        if cdate is None:
            cfunc = partial(_set_stock_info, cdate, bonus_info, index_info)
            return process_concurrent_run(cfunc, failed_list, num = 5)
        else:
            succeed = True
            start_date = get_day_nday_ago(cdate, num = 1, dformat = "%Y-%m-%d")
            for mdate in get_dates_array(start_date, cdate, asending = True):
                if self.cal_client.is_trading_day(mdate):
                    cfunc = partial(_set_stock_info, mdate, bonus_info, index_info)
                    if not process_concurrent_run(cfunc, failed_list, num = 500):
                        succeed = False
            return succeed

    def init_industry_info(self, cdate):
        def _set_industry_info(cdate, code_id):
            return (code_id, CIndex(code_id).set_k_data(cdate))
        df = self.industry_info_client.get()
        if cdate is None:
            cfunc = partial(_set_industry_info, cdate)
            return concurrent_run(cfunc, df.code.tolist(), num = 5)
        else:
            succeed = True
            start_date = get_day_nday_ago(cdate, num = 30, dformat = "%Y-%m-%d")
            for mdate in get_dates_array(start_date, cdate, asending = True):
                if self.cal_client.is_trading_day(mdate):
                    cfunc = partial(_set_industry_info, mdate)
                    if not concurrent_run(cfunc, df.code.tolist(), num = 5):
                        succeed = False
            return succeed

    def init_yesterday_hk_info(self, cdate):
        succeed = True
        for data in ((ct.SH_MARKET_SYMBOL, ct.HK_MARKET_SYMBOL), (ct.SZ_MARKET_SYMBOL, ct.HK_MARKET_SYMBOL)):
            if not self.connect_client.set_market(data[0], data[1]):
                self.logger.error("connect_client for %s failed" % data)
                succeed = False
                continue

            if not self.connect_client.update(cdate):
                succeed = False

            self.connect_client.close()
            self.connect_client.quit()
        kill_process("zygote")
        kill_process("defunct")
        kill_process("show-component-extension-options")
        return succeed

    def init_index_components_info(self, cdate = None):
        if cdate is None: cdate = datetime.now().strftime('%Y-%m-%d')
        def _set_index_info(code_id):
            _obj = self.index_objs[code_id] if code_id in self.index_objs else CIndex(code_id)
            return (code_id, _obj.set_components_data(cdate))
        return concurrent_run(_set_index_info, list(ct.INDEX_DICT.keys()), num = 10)

    def set_bull_stock_ratio(self, cdate, num = 10):
        def _set_bull_stock_ratio(code_id):
            return (code_id, BullStockRatio(code_id).update(cdate))
        return concurrent_run(_set_bull_stock_ratio, list(ct.INDEX_DICT.keys()), num = num)

    def init_tdx_index_info(self, cdate = None):
        def _set_index_info(cdate, code_id):
            try:
                _obj = self.index_objs[code_id] if code_id in self.index_objs else CIndex(code_id)
                return (code_id, _obj.set_k_data(cdate))
            except Exception as e:
                self.logger.error(e)
                return (code_id, False)
        if cdate is None:
            cfunc = partial(_set_index_info, cdate)
            return concurrent_run(cfunc, list(ct.TDX_INDEX_DICT.keys()), num = 5)
        else:
            succeed = True
            start_date = get_day_nday_ago(cdate, num = 30, dformat = "%Y-%m-%d")
            for mdate in get_dates_array(start_date, cdate, asending = True):
                if self.cal_client.is_trading_day(mdate):
                    cfunc = partial(_set_index_info, mdate)
                    if not concurrent_run(cfunc, list(ct.TDX_INDEX_DICT.keys()), num = 5):
                        succeed = False
            return succeed

    def download_and_extract(self, cdate):
        try:
            if not download(ct.ZIP_DIR, cdate): return False
            list_files = os.listdir(ct.ZIP_DIR)
            for filename in list_files:
                if not filename.startswith('.'):
                    file_path = os.path.join(ct.ZIP_DIR, filename)
                    if os.path.exists(file_path):
                        unzip(file_path, ct.TIC_DIR)
            return True
        except Exception as e:
            self.logger.error(e)
            return False
 
if __name__ == '__main__':
    #from cmysql import CMySQL
    #mysql_client = CMySQL(dbinfo = ct.DB_INFO)
    #mysql_client.delete_db(ct.RINDEX_STOCK_INFO_DB)
    #import sys
    #sys.exit(0)
    #for code in IndustryInfo().get().code.tolist():
    #    print(code)
    #    mysql_client.delete_db('i%s' % code)
    #for code in CStockInfo().get().code.tolist():
    #    print(code)
    #    mysql_client.delete_db('s%s' % code)
    #import sys
    #sys.exit(0)

    kill_process("google-chrome")
    kill_process("renderer")
    kill_process("Xvfb")
    kill_process("zygote")
    kill_process("defunct")
    kill_process("show-component-extension-options")

    dm = DataManager()
    dm.logger.info("start compute!")
    #dm.bootstrap(exec_date = '2019-01-25')
    dm.bootstrap(cdate='2019-02-18', exec_date = '2019-02-18')
    dm.logger.info("end compute!")
