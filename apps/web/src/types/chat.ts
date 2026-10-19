export type RagQuery = {question:string;k:number;chat_provider_id:string;chat_model_id:string;embedding_profile_id:string;meeting_ids?:string[]};
export type ProviderCatalog = {max_k:number;providers:{id:string;name:string;is_external:boolean;chat_models:string[]}[];embedding_profiles:{id:string;provider_id:string;model:string;revision:string}[]};
export type Citation = {source_id:string;meeting_id:string;meeting_title:string;speaker:string;start_seconds:number;end_seconds:number;excerpt:string};
export type RagAnswer = {answer:string;retrieval_status:'answered'|'insufficient_evidence';prompt_version:string;selected_meetings:{meeting_id:string;title:string;relevance_score:number}[];citations:Citation[]};
