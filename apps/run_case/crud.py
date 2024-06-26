#! /usr/bin/python3
# -*- coding: utf-8 -*-

"""
@Author: Kobayasi
@File: crud.py
@Time: 2022/8/23-13:56
"""

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from apps.case_service import models as service_case


async def update_test_case_order(db: AsyncSession, case_id: int, is_fail: bool):
    """
    更新用例次数
    :param db:
    :param case_id:
    :param is_fail: 是否失败
    :return:
    """
    result = await db.execute(
        select(service_case.TestCase).filter(service_case.TestCase.id == case_id)
    )
    db_case = result.scalars().first()
    db_case.run_order = db_case.run_order + 1
    if is_fail:
        db_case.fail = db_case.fail + 1
    else:
        db_case.success = db_case.success + 1

    await db.commit()
    await db.refresh(db_case)
    return db_case
