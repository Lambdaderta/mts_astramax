import { createApiService } from './api/service';
import { chatService } from './service';

// /demo always creates its own mock service, independently of this selection.
export const appService = import.meta.env.VITE_DATA_SOURCE === 'api'
  ? createApiService({
    baseUrl: import.meta.env.VITE_API_BASE_URL || '/api/v1',
    reviewEnabled: import.meta.env.VITE_ENABLE_REVIEW === 'true',
    reviewBaseUrl: import.meta.env.VITE_REVIEW_BASE_URL || '/review-api/v1',
  })
  : chatService;
