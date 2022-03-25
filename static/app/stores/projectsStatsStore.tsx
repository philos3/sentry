import Reflux from 'reflux';

import ProjectActions from 'sentry/actions/projectActions';
import {Project} from 'sentry/types';
import {makeSafeRefluxStore} from 'sentry/utils/makeSafeRefluxStore';

type ProjectsStatsStoreInterface = {
  getAll(): ProjectsStatsStoreInterface['itemsBySlug'];

  getBySlug(slug: string): Project;
  getInitialState(): ProjectsStatsStoreInterface['itemsBySlug'];
  itemsBySlug: Record<string, Project>;
  reset(): void;
};

/**
 * This is a store specifically used by the dashboard, so that we can
 * clear the store when the Dashboard unmounts
 * (as to not disrupt ProjectsStore which a lot more components use)
 */
const storeConfig: Reflux.StoreDefinition & ProjectsStatsStoreInterface = {
  itemsBySlug: {},

  init() {
    this.reset();
    this.listenTo(ProjectActions.loadStatsForProjectSuccess, this.onStatsLoadSuccess);
    this.listenTo(ProjectActions.update, this.onUpdate);
    this.listenTo(ProjectActions.updateError, this.onUpdateError);
  },

  getInitialState() {
    return this.itemsBySlug;
  },

  reset() {
    this.itemsBySlug = {};
    this.updatingItems = new Map();
  },

  onStatsLoadSuccess(projects: Project[]) {
    projects.forEach(project => {
      this.itemsBySlug[project.slug] = project;
    });
    this.trigger(this.itemsBySlug);
  },

  /**
   * Optimistic updates
   * @param projectSlug Project slug
   * @param data Project data
   */
  onUpdate(projectSlug: string, data: Project) {
    const project = this.getBySlug(projectSlug);
    this.updatingItems.set(projectSlug, project);
    if (!project) {
      return;
    }

    const newProject: Project = {
      ...project,
      ...data,
    };

    this.itemsBySlug = {
      ...this.itemsBySlug,
      [project.slug]: newProject,
    };
    this.trigger(this.itemsBySlug);
  },

  onUpdateSuccess(data: Project) {
    // Remove project from updating map
    this.updatingItems.delete(data.slug);
  },

  /**
   * Revert project data when there was an error updating project details
   * @param err Error object
   * @param data Previous project data
   */
  onUpdateError(_err: Error, projectSlug: string) {
    const project = this.updatingItems.get(projectSlug);
    if (!project) {
      return;
    }

    this.updatingItems.delete(projectSlug);
    // Restore old project
    this.itemsBySlug = {
      ...this.itemsBySlug,
      [project.slug]: {...project},
    };
    this.trigger(this.itemsBySlug);
  },

  getAll() {
    return this.itemsBySlug;
  },

  getBySlug(slug) {
    return this.itemsBySlug[slug];
  },
};

const ProjectsStatsStore = makeSafeRefluxStore(
  Reflux.createStore(storeConfig) as Reflux.Store & ProjectsStatsStoreInterface
);

export default ProjectsStatsStore;
