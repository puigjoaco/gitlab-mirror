// Google Apps Script clock for the GitHub → GitLab mirror.
// GitHub's own schedule can lag by hours, so this time trigger starts the
// mirror workflow every 10 minutes. Script property GITHUB_DISPATCH_TOKEN holds
// a fine-grained GitHub token limited to puigjoaco/gitlab-mirror with
// Actions: read and write. Run installTrigger() once after pasting this file.

const REPOSITORY = 'puigjoaco/gitlab-mirror';
const WORKFLOW = 'mirror.yml';

function dispatchMirror() {
  const token = PropertiesService.getScriptProperties().getProperty('GITHUB_DISPATCH_TOKEN');
  if (!token) throw new Error('Script property GITHUB_DISPATCH_TOKEN is missing');
  const response = UrlFetchApp.fetch(
    'https://api.github.com/repos/' + REPOSITORY + '/actions/workflows/' + WORKFLOW + '/dispatches', {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify({ ref: 'main' }),
      headers: {
        Authorization: 'Bearer ' + token,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      muteHttpExceptions: true,
    });
  const status = response.getResponseCode();
  // A failure makes Google email the account owner about the failed trigger.
  if (status !== 204) throw new Error('GitHub dispatch failed: HTTP ' + status);
}

function installTrigger() {
  ScriptApp.getProjectTriggers()
    .filter(trigger => trigger.getHandlerFunction() === 'dispatchMirror')
    .forEach(trigger => ScriptApp.deleteTrigger(trigger));
  ScriptApp.newTrigger('dispatchMirror').timeBased().everyMinutes(10).create();
}
