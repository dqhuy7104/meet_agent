import type { Role } from './projectApi';

export const roleLabels: Record<Role, string> = {
  system_admin: 'Admin',
  project_manager: 'Project Manager',
  member: 'Thành viên',
  viewer: 'Chỉ xem',
};
