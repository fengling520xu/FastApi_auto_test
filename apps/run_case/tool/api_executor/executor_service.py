#! /usr/bin/env python3
# -*- coding: UTF-8 -*-

"""
@File    ：executor_service.py
@IDE     ：PyCharm 
@Author  ：Kobayasi
@Date    ：2024/3/22 11:26 
"""

import copy
import time
import json
import random
import base64
import asyncio
import aiohttp
import jsonpath
from sqlalchemy.ext.asyncio import AsyncSession
from aiohttp import FormData, client_exceptions
from apps.case_service import crud as case_crud
from apps.template import crud as temp_crud
from apps.api_report import crud as report_crud
from apps.api_report import schemas as report_schemas
from apps.run_case import crud as run_crud
from apps.case_service.tool import jsonpath_count
from apps.run_case import CASE_STATUS, CASE_RESPONSE, CASE_STATUS_LIST
from tools import logger, get_cookie, AsyncMySql
from tools.read_setting import setting

from .base_abstract import ApiBase
from ..del_status import del_status
from ..handle_headers import replace_headers
from ..run_api_data_processing import DataProcessing
from ..check_data import check_customize
from ..assert_case import AssertCase


class ExecutorService(ApiBase):

    def __init__(self, db: AsyncSession):
        super(ExecutorService, self).__init__()
        self._db = db
        self._case_group = {}
        self._setting_info_dict = {}
        self._cookie = {}

    async def collect_sql(self, **kwargs):
        """
        将需要用到的数据，一次性查询出来
        :return:
        """
        # 查询用例数据
        # todo 相同的id重复执行，查出来的数据是一样的，要处理
        case_data = await case_crud.get_case_data_group(self._db, case_ids=kwargs['case_ids'])
        case_ids = {x[0].id for x in case_data}
        if len(case_ids) != len(kwargs['case_ids']):
            raise ValueError(
                f"不存在的用例: {list(set(kwargs['case_ids']) - case_ids)}"
            )
        temp_data = await temp_crud.get_temp_data_group(
            db=self._db,
            temp_ids=list(set([x[0].temp_id for x in case_data]))
        )

        # 按用例id进行分组
        case_group = {}
        for case in case_data:
            if case[0].id not in case_group:
                case_group[case[0].id] = [list(case)]
            else:
                case_group[case[0].id].append(list(case))

        # 模板加入到用例的分组
        temp_data_dict = {(x[1].temp_id, x[1].number): x for x in temp_data}
        for k, v in case_group.items():
            for i, v_ in enumerate(v):
                case_group[k][i] += list(temp_data_dict[(v_[0].temp_id, v_[1].number)])

        # 按用户请求顺序排序
        # {case_id： [(用例，用例详情，模板，模板详情)]}
        self._case_group = {k: case_group[k] for k in kwargs['case_ids']}

    async def collect_config(self, setting_info_dict):
        """
        拿临时的执行配置信息
        :return:
        """
        self._setting_info_dict = setting_info_dict

    async def collect_req_data(self):
        """
        收集请求数据
        :return:
        """
        api_group = []
        for _, case_data in self._case_group.items():
            api_list = []
            for case in case_data:
                api_data = {
                    'api_info': {
                        'host': case[3].host,
                        'case_id': case[1].case_id,
                        'number': case[1].number,
                        'temp_name': case[2].temp_name,
                        'case_name': case[0].case_name,
                        'description': case[1].description,
                        'json_body': 'json' if case[3].json_body == 'json' else 'data',
                        'file': case[3].file,
                        'file_data': case[3].file_data,
                        'run_status': True  # 执行中ture， 停止false
                    },
                    'history': {
                        'path': case[1].path,
                        'params': case[1].params,
                        'data': case[1].data,
                        'headers': case[1].headers,
                        'response': case[3].response,
                        'response_headers': case[3].response_headers
                    },
                    'request_info': {
                        'url': f'{case[3].host}{case[1].path}',
                        'method': case[3].method,
                        'headers': case[3].headers,
                        'params': case[1].params,
                        f"{'json' if case[3].json_body == 'json' else 'data'}": case[1].data,
                    },
                    'response_info': [],  # 可能会存在单接口多次请求的情况
                    'assert_info': [],  # 校验结果同理
                    'report': {
                        'result': 0,  # 成功0、失败1、跳过2
                        'is_executor': None,  # True 执行，False 跳过, None 人为终止
                    },
                    'config': case[1].config,
                    'check': case[1].check,
                    'jsonpath_info': [],
                    # 扩展字段
                    'other_info': {
                        'description': case[1].description if case[1].description else '--',
                        'timestamp': time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time())),
                        'file': True if case[3].file else False,
                    }
                }

                api_list.append(api_data)
            api_group.append(api_list)
        self.api_group = copy.deepcopy(api_group)

    async def executor_api(self, sync: bool = True):
        """
        按同步或异步执行用例
        :return:
        """
        if sync:
            for api_list in self.api_group:
                try:
                    await self._run_api(api_list=api_list, key_id=f'{time.time()}_{random.uniform(0, 1)}')
                except client_exceptions.ClientConnectorError as e:
                    logger.error(e)
        else:
            tasks = [
                asyncio.create_task(
                    self._run_api(
                        api_list=api_list,
                        key_id=f'{time.time()}_{random.uniform(0, 1)}'
                    )
                ) for api_list in self.api_group
            ]
            await asyncio.gather(*tasks)

    async def collect_report(self):
        """
        处理执行结果报告
        :return:
        """

        run_numbers = await report_crud.get_max_run_number(db=self._db, case_ids=list(self._case_group.keys()))
        run_numbers = {k: v for k, v in run_numbers}

        for api_list in self.api_group:
            report = {
                'case_id': api_list[0]['api_info']['case_id'],
                'run_number': run_numbers.get(api_list[0]['api_info']['case_id'], 0) + 1,
                'total_api': len(api_list),
                'initiative_stop': False,
                'fail_stop': False,
                'result': {
                    'run_api': 0,
                    'success': 0,
                    'fail': 0,
                    'skip': 0,
                    'result': 0  # 成功0、失败1、跳过2,
                },
                'time': {
                    'total_time': 0.0,
                    'max_time': 0.0,
                    'avg_time': 0.0,
                }
            }

            for api in api_list:
                # 如果是none，代表人为终止
                if api['report']['is_executor'] is None:
                    break

                if api['report']['is_executor']:
                    report['result']['run_api'] += 1

                if api['report']['result'] == 0:
                    report['result']['success'] += 1

                if api['report']['result'] == 1:
                    report['result']['result'] = 1
                    report['result']['fail'] += 1

                if api['report']['result'] == 2:
                    report['result']['skip'] += 1

                if api['config'].get('stop'):
                    report['initiative_stop'] = True

                if api['config'].get('fail_stop'):
                    report['fail_stop'] = True
                response_time = api['response_info'][-1]['response_time']
                max_time = report['time']['max_time']
                report['time']['total_time'] += response_time
                report['time']['max_time'] = response_time if response_time > max_time else max_time

                # 获取jsonpath数据
                class Case:
                    number = api['api_info']['number']
                    path = api['history']['path']
                    params = api['history']['params']
                    data = api['history']['data']
                    headers = api['history']['headers']
                    check = api['check']

                api['jsonpath_info'] = jsonpath_count(
                    case_list=[Case],
                    temp_list=[],
                    run_case=api_list
                )

                # 附件较大，不保留到日志中，仅保留概要信息
                if api['api_info']['file']:
                    api['request_info']['data'] = [
                        {
                            'name': file['name'],
                            'content_type': file['contentType'],
                            'filename': file['fileName']

                        } for file in api['api_info']['file_data']
                    ]
                    api['api_info']['file_data'] = []
            else:
                report['time']['avg_time'] = report['time']['total_time'] / report['result']['run_api']

            # 写入报告列表
            db_data = await report_crud.create_api_list(db=self._db, data=report_schemas.ApiReportListInt(**report))
            # 写入详情列表
            await report_crud.create_api_detail(
                db=self._db,
                data=[x for x in api_list if x['report']['is_executor'] is not None],
                report_id=db_data.id
            )
            # 更新用例次数
            await run_crud.update_test_case_order(
                db=self._db,
                case_id=report['case_id'],
                is_fail={0: False, 1: True}.get(report['result']['result'])
            )

            self.report_list.append(report)
            CASE_RESPONSE[report['case_id']] = copy.deepcopy(api_list)

    async def _run_api(self, api_list: list, key_id: str):
        """
        执行用例
        :param api_list:
        :param key_id:
        :return:
        """
        sees = aiohttp.client.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
        data_processing = DataProcessing(db=self._db)
        logger.info(
            f"{'=' * 30}{api_list[0]['api_info']['temp_name']}-{api_list[0]['api_info']['case_name']}{'=' * 30}"
        )
        for i, api in enumerate(api_list):
            # 处理请求相关的jsonpath数据
            (
                api['request_info']['url'],
                api['request_info']['params'],
                api['request_info'][api['api_info']['json_body']],
                api['request_info']['headers'],
                api['check']
            ) = await data_processing.processing(
                url=api['request_info']['url'],
                params=api['request_info']['params'],
                data=api['request_info'].get('data') or api['request_info'].get('json'),
                headers=replace_headers(  # 将用例中的headers临时替换到模板中
                    cookie=self._cookie.get(
                        api['api_info']['host'],
                        api['history']['headers'].get(
                            'Cookie',
                            api['history']['headers'].get(
                                'cookie',
                                api['request_info']['headers'].get(
                                    'Cookie',
                                    api['request_info']['headers'].get('cookie', '')
                                )
                            )
                        ) if not api['config'].get('is_login') else ''
                    ),
                    tmp_header=api['request_info']['headers'],
                    case_header=api['history']['headers'],
                    tmp_file=api['api_info']['file']
                ),
                check=api['check'],
                api_list=api_list,
                customize=await check_customize(self._setting_info_dict.get('customize', {})),
            )

            # 处理附件上传
            if api['api_info']['file']:
                files_data = FormData()
                for file in api['api_info']['file_data']:
                    files_data.add_field(
                        name=file['name'],
                        value=base64.b64decode(file['value'].encode('utf-8')),
                        content_type=file['contentType'],
                        filename=file['fileName'].encode().decode('unicode_escape')
                    )
                api['request_info']['data'] = files_data

            # 跳过用例执行
            if api['config'].get('skip'):
                api['response_info'] = [
                    {
                        'status_code': 0,
                        'response_time': 0,
                        'response': {},
                        'headers': {},
                    }
                ]
                result = await self._assert(
                    check=api['check'],
                    response=api['response_info'][-1]['response'],
                    skip=True
                )
                api['assert_info'].append(result)

            # ⬜️================== 🍉轮询发起请求，单接口的默认间隔时间超过5s，每次请求间隔5s进行轮询🍉 ==================⬜️ #
            sleep = api['config']['sleep']
            res = None
            while not api['config'].get('skip'):
                response_info = {
                    'status_code': 0,
                    'response_time': 0,
                    'response': {},
                    'headers': {},
                }
                start_time = time.monotonic()
                try:
                    res = await sees.request(**api['request_info'], allow_redirects=False)
                except client_exceptions.ClientError:
                    response_info['response_time'] = time.monotonic() - start_time
                    response_info['response']['status_code'] = 9999
                    response_info['status_code'] = 9999
                else:
                    try:
                        response_info['response'] = await res.json(
                            content_type='application/json' if not api['api_info']['file'] else None
                        ) or {}
                    except (client_exceptions.ContentTypeError, json.decoder.JSONDecodeError):
                        response_info['response'] = {}
                    # 循环请求中的信息收集
                    response_info['status_code'] = res.status
                    response_info['response_time'] = time.monotonic() - start_time
                    response_info['headers'] = dict(res.headers)

                    # 断言结果
                    if not isinstance(response_info['response'], (dict, list)):
                        response_info['response'] = [{'status_code': res.status}, response_info['response']]
                    else:
                        response_info['response']['status_code'] = res.status

                api['response_info'].append(response_info)

                # 处理响应
                result = await self._assert(check=api['check'], response=response_info['response'])
                api['assert_info'].append(result)
                try:
                    del response_info['response']['status_code']
                except TypeError:
                    response_info['response'] = response_info['response'][-1]

                # 判断退出while条件
                if any([
                    sleep <= 5,
                    CASE_STATUS.get(key_id, {}).get('stop'),
                    not [x for x in api['assert_info'][-1] if x['result'] == 1]  # 判断断言结果，没有失败则退出循环，不继续轮询
                ]):
                    break
                else:
                    if i <= len(api_list) - 2:
                        await self._case_status(api=api, key_id=key_id, total=len(api_list), retry=True)
                    sleep -= 5
                    await asyncio.sleep(5)
            # ⬜️================== 🍉轮询结束请求，单接口的默认间隔时间超过5s，每次请求间隔5s进行轮询🍉 ==================⬜️ #

            # 记录cookie
            if res and api['config'].get('is_login'):
                self._cookie[api['api_info']['host']] = await get_cookie(rep_type='aiohttp', response=res)

            # 轮询结束后，记录单接口执行结果
            api['report']['result'] = self._assert_info(api['assert_info'][-1])
            api['report']['is_executor'] = True if not api['config'].get('skip') else False
            logger.info(
                f"{api['api_info']['case_id']}-({api['api_info']['number']}/{len(api_list) - 1})-"
                f"{api['request_info']['url']} {dict({0: 'SUCCESS', 1: 'FAIL', 2: 'SKIP'}).get(api['report']['result'])}"
            )

            # 退出循环执行的判断
            if any([
                # 主动停止
                api['config'].get('stop'),
                # 手动停止
                CASE_STATUS.get(key_id, {}).get('stop'),
                # 执行失败停止
                all([
                    setting['global_fail_stop'],  # 配置中的失败停止总开关：开
                    api['config'].get('fail_stop'),  # 单接口配置失败停止：开
                    api['report']['result'] == 1  # 单接口结果：失败
                ]),
            ]):
                api['api_info']['run_status'] = False  # 标记停止运行的接口
                if i <= len(api_list) - 2:
                    await self._case_status(api=api, key_id=key_id, total=len(api_list))
                await sees.close()
                break

            if api['config'].get('sleep') <= 5:
                await asyncio.sleep(api['config']['sleep'])  # 业务场景用例执行下，默认的间隔时间

            if i <= len(api_list) - 2:
                await self._case_status(api=api, key_id=key_id, total=len(api_list))
        else:
            api_list[-1]['api_info']['run_status'] = False  # 标记停止运行的接口
            await self._case_status(api=api_list[-1], key_id=key_id, total=len(api_list))

        asyncio.create_task(del_status(key_id=key_id))
        await sees.close()

    async def _assert(self, check: dict, response: dict, skip: bool = False):
        """
        校验结果
        :param check:
        :param response:
        :param skip:
        :return:
        """
        result = []
        for k, v in check.items():
            if isinstance(v, list) and 'sql_' == k[:4]:
                # 从数据库获取实际的值
                sql_data = await self._sql_data(v[1], self._setting_info_dict.get('db', {}))
                value = sql_data[0]
            else:
                # 从响应信息获取需要的值
                value = jsonpath.jsonpath(response, f'$..{k}')
                if value:
                    value = value[0]

            # 校验结果
            is_fail = await AssertCase.assert_case(
                compare='==' if isinstance(v, (str, int, float, bool, dict)) else v[0],
                expect=v if isinstance(v, (str, int, float, bool, dict)) else v[1],
                actual=value,
            )
            result.append({
                "key": k,
                "actual": value,
                "compare": v[0] if not isinstance(v, (str, int, float, bool, dict)) else '==',
                "expect": v[1] if not isinstance(v, (str, int, float, bool, dict)) else v,
                "result": 2 if skip else {True: 1, False: 0}.get(is_fail),
            })

        return result

    @staticmethod
    async def _sql_data(sql: str, db_config: dict):
        """
        从数据库查询数据
        :param sql:
        :param db_config:
        :return:
        """
        if db_config.get('name'):
            del db_config['name']

        async with AsyncMySql(db_config) as s:
            sql_data = await s.select(sql=sql)
            return [x[0] for x in sql_data] if sql_data else False

    @staticmethod
    def _assert_info(assert_info: list):
        info_list = [x['result'] for x in assert_info]
        if 2 in info_list:
            return 2
        if 1 in info_list:
            return 1
        return 0

    async def _case_status(self, api: dict, key_id: str, total: int, retry: bool = False):
        """
        记录用例运行状态
        :param api:
        :param key_id:
        :param total:
        :param retry:
        :return:
        """

        # 进度条
        stop = CASE_STATUS.get(key_id, {}).get('stop', False)
        number = CASE_STATUS.get(key_id, {}).get('number', 99999)

        # 计算成功失败
        success = CASE_STATUS.get(key_id, {}).get('success', 0)
        fail = CASE_STATUS.get(key_id, {}).get('fail', 0)
        skip = CASE_STATUS.get(key_id, {}).get('skip', 0)

        CASE_STATUS[key_id] = {
            'key_id': key_id,
            'case_id': api['api_info']['case_id'],
            'number': api['api_info']['number'],
            'success': success + 1 if all([
                [x for x in api['assert_info'][-1] if x['result'] == 0],
                retry is False,
                number != api['api_info']['number']
            ]) else success,
            'fail': fail + 1 if all([
                [x for x in api['assert_info'][-1] if x['result'] == 1],
                retry is False,
            ]) else fail,
            'skip': skip + 1 if all([
                [x for x in api['assert_info'][-1] if x['result'] == 2],
                retry is False,
            ]) else skip,
            'retry': retry,
            'total': total,
            'stop': stop,
            'run_status': api['api_info']['run_status'],
            'case_name': api['api_info']['case_name']
        }

        # 执行过程详情
        info = {
            'number': api['api_info']['number'],
            'url': api['request_info']['url'],
            'method': api['request_info']['method'],
            'status_code': api['response_info'][-1]['status_code'],
            'run_time': api['response_info'][-1]['response_time'],
            'is_fail': {0: False, 1: True, 2: None}.get(self._assert_info(api['assert_info'][-1])),
            'is_login': api['config'].get('is_login'),
            'description': api['api_info']['description'],
            'run_status': api['api_info']['run_status'],
            'total': total,
        }
        if CASE_STATUS_LIST.get(key_id):
            CASE_STATUS_LIST[key_id].append(info)
        else:
            CASE_STATUS_LIST[key_id] = [info]
