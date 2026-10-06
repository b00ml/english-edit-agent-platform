import axios from 'axios'
import { getToken } from './auth'
import type { KnowledgePreviewResult } from './types'

export interface OCRBlock {
  block_id: string; block_type: string; text: string; page_no: number | null
  source_locator: { bbox?: number[] | null; bbox_frame?: string | null }
  meta: { rows?: string[][]; table_structure_degraded?: boolean }
}
export interface OCRJob {
  id: string; filename: string; status: string; index_status: string; index_error: string | null
  page_count: number; selected_pages: number[]; completed_pages: number; total_pages: number
  cached_pages: number; progress: number; preview_available: boolean; partial_document: boolean
  review_settings?: { excluded_block_ids?: string[]; knowledge_points?: string[]; source_type?: string; review_note?: string; chunk_layout?: 'legacy' | 'structure'; accepted_edge_ids?: string[]; rejected_edge_ids?: string[] }
  index_revision: number; index_present: boolean; indexed_document_id: string | null; error_message: string | null
  pages?: { page_no: number; status: string; attempts: number; cache_hit: boolean; error_message: string | null }[]
}
export interface OCRReview extends KnowledgePreviewResult {
  document: KnowledgePreviewResult['document'] & { blocks: OCRBlock[]; source_hash: string; stats: { partial_document: boolean; excluded_block_ids?: string[] } }
  preview_hash: string; plan_hash: string
  embedding: { model: string; dimension: number; text_count: number; characters: number; price_configured: boolean }
}
const api = axios.create({ baseURL: '/api/knowledge/ocr/jobs', timeout: 120000 })
api.interceptors.request.use(config => { config.headers.Authorization = `Bearer ${getToken()}`; return config })
export const listOCRJobs = (page = 1) => api.get<{ total: number; items: OCRJob[] }>('', { params: { page, page_size: 20 } }).then(r => r.data)
export const getOCRJob = (id: string, page = 1) => api.get<OCRJob>(`/${id}`, { params: { page, page_size: 100 } }).then(r => r.data)
export const cancelOCR = (id: string) => api.post<OCRJob>(`/${id}/cancel`).then(r => r.data)
export const resumeOCR = (id: string) => api.post<OCRJob>(`/${id}/resume`).then(r => r.data)
export const getOCRReview = (id: string, excluded: string[] = [], accepted: string[] = [], rejected: string[] = [], chunkLayout?: 'legacy' | 'structure') => api.post<OCRReview>(`/${id}/review-plan`, { excluded_block_ids: excluded, accepted_edge_ids: accepted, rejected_edge_ids: rejected, chunk_layout: chunkLayout }).then(r => r.data)
export const importOCR = (files: string[], pages: number[] | null) => api.post<{ accepted: OCRJob[]; errors: { file: string; message: string }[] }>('/import-local', { files, pages }).then(r => r.data)
export async function uploadOCR(file: File, pages: number[] | null) {
  const form = new FormData(); form.append('file', file); form.append('pages', JSON.stringify(pages))
  return api.post<OCRJob>('/upload', form).then(r => r.data)
}
export const getOCRImage = async (id: string, page: number, signal: AbortSignal) => {
  const response = await api.get<Blob>(`/${id}/source-pages/${page}`, { responseType: 'blob', signal })
  return { blob: response.data, match: response.headers['x-ocr-coordinate-match'] === 'true' }
}
export const approveOCR = (id: string, data: {
  preview_hash: string; plan_hash: string; source_reviewed: boolean; warnings_acknowledged: boolean
  paid_embedding_acknowledged: boolean; accept_selected_pages: boolean; retry_authorized: boolean
  source_type: string; knowledge_points: string[]; review_note: string; excluded_block_ids: string[]
  chunk_layout?: 'legacy' | 'structure'
  rebuild_index?: boolean; expected_index_revision?: number
  accepted_edge_ids?: string[]; rejected_edge_ids?: string[]
}) => api.post<OCRJob>(`/${id}/approve-index`, data).then(r => r.data)


export interface BoundaryReviewJob {id:string; job_id:string; pages:number[]; status:string; attempts:number; cache_hit:boolean; result_available:boolean; error:string|null}
export const listBoundaryReviews=(job:string)=>api.get<{items:BoundaryReviewJob[]}>(`/${job}/boundary-reviews`).then(r=>r.data)
export const createBoundaryReview=(job:string,pages:number[])=>api.post<BoundaryReviewJob>(`/${job}/boundary-reviews`,{pages,local_compute_acknowledged:true}).then(r=>r.data)
export const controlBoundaryReview=(job:string,id:string,action:'cancel'|'resume')=>api.post<BoundaryReviewJob>(`/${job}/boundary-reviews/${id}/${action}`).then(r=>r.data)
export const getBoundaryResult=(job:string,id:string)=>api.get<{global_pages:number[];engine_version:string;structure:import('./types').StructurePlan;notice:string;source_applied:boolean;document:{blocks:OCRBlock[]}}>(`/${job}/boundary-reviews/${id}/result`).then(r=>r.data)
