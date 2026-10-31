from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from app.api.auth import CurrentAccount, require_account, require_csrf
from app.api.deps import get_conversation_service
from app.api.local_access import require_loopback
from app.api.routes.chat import SafeRagRoute
from app.schemas.conversations import ConversationCreate, ConversationDetail, ConversationList, ConversationSummary, ConversationTurn, TurnRequest


class ConversationRoute(SafeRagRoute):
    def get_route_handler(self):
        handler = super().get_route_handler()
        async def no_store(request: Request):
            response = await handler(request)
            response.headers['Cache-Control'] = 'no-store'
            return response
        return no_store


router = APIRouter(prefix='/chat/conversations',tags=['conversations'],route_class=ConversationRoute,
                   dependencies=[Depends(require_loopback)])
Account = Annotated[CurrentAccount,Depends(require_account)]
ConversationId = Annotated[str,Path(min_length=1,max_length=200)]


@router.get('',response_model=ConversationList)
async def list_conversations(account: Account, view: Literal['mine','others'] = 'mine',
                             cursor: Annotated[str | None,Query(max_length=512)] = None,
                             service=Depends(get_conversation_service)) -> ConversationList:
    return await service.list_conversations(view,cursor,account.id)


@router.post('',response_model=ConversationSummary,status_code=201,dependencies=[Depends(require_csrf)])
async def create_conversation(body: ConversationCreate, account: Account,
                              service=Depends(get_conversation_service)) -> ConversationSummary:
    return await service.create_conversation(body,account.id)


@router.get('/{conversation_id}',response_model=ConversationDetail)
async def get_conversation(conversation_id: ConversationId, account: Account,
                            before_ordinal: Annotated[int | None,Query(ge=1)] = None,
                            service=Depends(get_conversation_service)) -> ConversationDetail:
    return await service.get_conversation(conversation_id,before_ordinal,account.id)


@router.patch('/{conversation_id}',response_model=ConversationSummary,dependencies=[Depends(require_csrf)])
async def update_conversation(conversation_id: ConversationId, body: ConversationCreate, account: Account,
                               service=Depends(get_conversation_service)) -> ConversationSummary:
    return await service.update_conversation(conversation_id,body,account.id)


@router.delete('/{conversation_id}',status_code=204,dependencies=[Depends(require_csrf)])
async def delete_conversation(conversation_id: ConversationId, account: Account,
                               service=Depends(get_conversation_service)) -> Response:
    await service.delete_conversation(conversation_id,account.id)
    return Response(status_code=204,headers={'Cache-Control':'no-store'})


@router.post('/{conversation_id}/turns',response_model=ConversationTurn,dependencies=[Depends(require_csrf)])
async def append_turn(conversation_id: ConversationId, body: TurnRequest, account: Account,
                       service=Depends(get_conversation_service)) -> ConversationTurn:
    return await service.append_turn(conversation_id,body,account.id)
