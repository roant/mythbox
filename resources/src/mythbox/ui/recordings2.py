#
#  MythBox for XBMC - http://mythbox.googlecode.com
#  Copyright (C) 2011 analogue@yahoo.com
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
import bidict
import datetime
import logging
import odict
import os
import time
import xbmc
import xbmcgui
import mythbox.msg as m

from mythbox.mythtv.conn import inject_conn
from mythbox.ui.recordingdetails import RecordingDetailsWindow
from mythbox.ui.toolkit import window_busy, BaseWindow, Action
from mythbox.util import catchall_ui, run_async, timed, catchall, ui_locked2, coalesce, safe_str
from mythbox.util import CyclingBidiIterator, formatSize

log = logging.getLogger('mythbox.ui')

ID_GROUPS_LISTBOX         = 700
ID_PROGRAMS_LISTBOX       = 600
ID_REFRESH_BUTTON         = 250
ID_SORT_BY_BUTTON         = 251
ID_SORT_ASCENDING_TOGGLE  = 252
ID_RECORDING_GROUP_BUTTON = 253

TITLE_SORT_BY = odict.odict([
    ('Date',           {'translation_id': m.DATE,          'reverse':True,  'sorter' : lambda x: x.starttimeAsTime() }), 
    ('Title',          {'translation_id': m.TITLE,         'reverse':False, 'sorter' : lambda x: '%s%s' % (x.title(), x.originalAirDate())}), 
    ('Orig. Air Date', {'translation_id': m.ORIG_AIR_DATE, 'reverse':True,  'sorter' : lambda x: x.originalAirDate()})])

GROUP_SORT_BY = odict.odict([
    ('Title', {'translation_id': m.TITLE, 'reverse': False, 'sorter' : lambda g: [g.title, '0000'][g.title == 'All recordings']}),
    ('Date',  {'translation_id': m.DATE,  'reverse': True,  'sorter' : lambda g: [g.programs[0].starttimeAsTime(), datetime.datetime(datetime.MAXYEAR, 12, 31, 23, 59, 59, 999999, tzinfo=None)][g.title == 'All recordings']})])

class Group(object):
    
    def __init__(self, title=None):
        self.title = title
        self.programs = []
        self.listItems = []
        self.programsByListItem = bidict.bidict()
        self.episodesDone = False
        self.postersDone = False
        self.backgroundsDone = False
        self.index = 0
        
    def add(self, program):
        if self.title is None:
            self.title = program.title()
        self.programs.append(program)
        
    def remove(self, program):
        self.programs.remove(program)

    def __str__(self):
        s = """
        group         = %s
        num programs  = %d 
        num listitems = %d
        num li map    = %d """ % (safe_str(self.title), len(self.programs), len(self.listItems), len(self.programsByListItem))
        return s


class RecordingsWindow(BaseWindow):
        
    def __init__(self, *args, **kwargs):
        BaseWindow.__init__(self, *args, **kwargs)
        # inject dependencies from constructor
        [setattr(self,k,v) for k,v in kwargs.iteritems() if k in ('settings', 'translator', 'platform', 'fanArt', 'cachesByName')]
        [setattr(self,k,v) for k,v in self.cachesByName.iteritems()]

        self.programs = []                       # [RecordedProgram]
        self.allGroupTitle = self.translator.get(m.ALL_RECORDINGS)
        self.activeRenderToken = None
        self.groupsByTitle = odict.odict()       # {unicode:Group}
        self.activeGroup = None
        self.lastFocusId = None
        self.sameBackgroundCache = {}            # {title:filepath}
        
    @catchall_ui
    def onInit(self):
        if not self.win:
            self.win = xbmcgui.Window(xbmcgui.getCurrentWindowId())
            self.groupsListbox = self.getControl(ID_GROUPS_LISTBOX)
            self.programsListbox = self.getControl(ID_PROGRAMS_LISTBOX)
            self.readSettings()
            self.refresh()
        self.initDone = True
        
    def readSettings(self):
        self.lastSelectedGroup = self.settings.get('recordings_selected_group')
        self.lastSelectedTitle = self.settings.get('recordings_selected_title')
        self.groupSortBy = self.settings.get('recordings_group_sort')
        self.titleSortBy = self.settings.get('recordings_title_sort')
        self.sortAscending = self.settings.getBoolean('recordings_sort_ascending')
        
    def onFocus(self, controlId):
        self.lastFocusId = controlId
        if controlId == ID_GROUPS_LISTBOX:
            log.warn('groups focus')
        else:
            log.warn('uncaught focus %s' % controlId)
            
    @catchall_ui
    def onClick(self, controlId):
        if controlId in (ID_GROUPS_LISTBOX, ID_PROGRAMS_LISTBOX,): 
            self.goRecordingDetails()
        elif controlId == ID_REFRESH_BUTTON:
            self.lastSelected = self.programsListbox.getSelectedPosition()
            self.refresh()
        elif controlId == ID_SORT_BY_BUTTON:
            keys = GROUP_SORT_BY.keys()
            self.groupSortBy = keys[(keys.index(self.groupSortBy) + 1) % len(keys)]
            self.applyGroupSort()

        elif controlId == ID_SORT_ASCENDING_TOGGLE:
            self.sortAscending = not self.sortAscending
            self.applySort()
        else:
            log.warn('uncaught onClick %s' % controlId)

    def saveSettings(self):
        if self.programs:
            try:
                group = self.groupsListbox.getSelectedItem().getProperty('title')
                self.settings.put('recordings_selected_group', [group, ''][group is None])
                title = self.programsListbox.getSelectedItem().getProperty('title')
                self.settings.put('recordings_selected_program', [title, ''][title is None])
            except:
                pass
            
        self.settings.put('recordings_title_sort', self.titleSortBy)
        self.settings.put('recordings_group_sort', self.groupSortBy)
        self.settings.put('recordings_sort_ascending', '%s' % self.sortAscending)
                                     
    @catchall_ui
    def onAction(self, action):
        id = action.getId()
        if id in (Action.PREVIOUS_MENU, Action.PARENT_DIR):
            self.closed = True
            self.saveSettings()
            self.close()
        elif id in (Action.UP, Action.DOWN, Action.PAGE_UP, Action.PAGE_DOWN, Action.HOME, Action.END):
            if self.lastFocusId == ID_GROUPS_LISTBOX:
                log.warn('groups select!')
                self.onGroupSelect()
            elif self.lastFocusId == ID_PROGRAMS_LISTBOX:
                log.warn('title select!')
                self.onTitleSelect()
        elif id == ID_GROUPS_LISTBOX:
            log.warn('groups action!')
        else:
            log.warn('uncaught action id %s' % id)
    
    def onTitleSelect(self):
        self.lastSelectedTitle = self.programsListbox.getSelectedItem().getProperty('title')
    
    @run_async
    @coalesce
    def preCacheThumbnails(self):
        if self.programs:
            log.debug('Precaching %d thumbnails' % len(self.programs))
            for program in self.programs[:]:
                if self.closed or xbmc.abortRequested: 
                    return
                try:
                    self.mythThumbnailCache.get(program)
                except:
                    log.exception('Thumbnail generation for recording %s failed' % safe_str(program.fullTitle()))

    @run_async
    @coalesce
    def preCacheCommBreaks(self):
        if self.programs:
            log.debug('Precaching %d comm breaks' % len(self.programs))
            for program in self.programs[:]:
                if self.closed or xbmc.abortRequested: 
                    return
                try:
                    if program.isCommFlagged():
                        program.getFrameRate()
                except:
                    log.exception('Comm break caching for recording %s failed' % safe_str(program.fullTitle()))

    @window_busy
    @inject_conn
    def refresh(self):
        self.programs = self.conn().getAllRecordings()
        if not self.programs:
            xbmcgui.Dialog().ok(self.translator.get(m.INFO), self.translator.get(m.NO_RECORDINGS_FOUND))
            self.close()
            return
        self.programs.sort(key=TITLE_SORT_BY[self.titleSortBy]['sorter'], reverse=TITLE_SORT_BY[self.titleSortBy]['reverse'])
        
        self.sameBackgroundCache.clear()
        self.preCacheThumbnails()

        # NOTE: No aggressive caching on windows since spawning the ffmpeg subprocess
        #       launches an annoying window
        if self.platform.getName() in ('unix','mac') and self.settings.isAggressiveCaching(): 
            self.preCacheCommBreaks()

        self.groupsByTitle.clear()
        self.groupsByTitle[self.allGroupTitle] = allGroup = Group(self.allGroupTitle)
        for p in self.programs:
            if not p.title() in self.groupsByTitle:
                self.groupsByTitle[p.title()] = Group()
            self.groupsByTitle[p.title()].add(p)
            allGroup.add(p)
            
        self.render()
    
    def applyGroupSort(self):
        if self.groupSortBy == 'Date':
            self.titleSortBy = 'Date'
        elif self.groupSortBy == 'Title':
            self.titleSortBy = 'Orig. Air Date'
            
        self.programs.sort(key=TITLE_SORT_BY[self.titleSortBy]['sorter'], reverse=TITLE_SORT_BY[self.titleSortBy]['reverse'])
        self.refresh()
        
    @ui_locked2
    def render(self):
        log.debug('Rendering....')
        self.renderNav()
        self.renderGroups()
        
    def renderNav(self):
        self.setWindowProperty('sortBy', self.translator.get(m.SORT) + ': ' + self.translator.get(GROUP_SORT_BY[self.groupSortBy]['translation_id']))
        self.setWindowProperty('sortAscending', ['false', 'true'][self.sortAscending])

    def renderGroups(self):
        lastSelectedIndex = 0
        listItems = []
        
        sortedGroups = self.groupsByTitle.values()[:]
        sortedGroups.sort(key=GROUP_SORT_BY[self.groupSortBy]['sorter'], reverse=GROUP_SORT_BY[self.groupSortBy]['reverse'])
                    
        for i, group in enumerate(sortedGroups):
            title = group.title
            group.listItem = xbmcgui.ListItem()
            group.index = i
            listItems.append(group.listItem)
            self.setListItemProperty(group.listItem, 'index', str(i))
            self.setListItemProperty(group.listItem, 'title', title)
            self.setListItemProperty(group.listItem, 'num_episodes', str(len(group.programs)))
            if self.lastSelectedGroup == title:
                lastSelectedIndex = i
                log.warn('Last selected group index = %s %s' % (title, lastSelectedIndex))

        self.groupsListbox.reset()
        self.groupsListbox.addItems(listItems)

        #
        # HACK ALERT: Selection won't register unless gui unlocked
        #        
        xbmcgui.unlock()
        self.selectListItemAtIndex(self.groupsListbox, lastSelectedIndex)
        xbmcgui.lock()
        
        log.warn('index checkl = %s' % self.groupsListbox.getSelectedPosition())
        self.onGroupSelect()

    def onGroupSelect(self, lsg=None):
        if not self.programs:
            return
        elif lsg is None:
            self.activeGroup = self.groupsByTitle[self.groupsListbox.getSelectedItem().getProperty('title')]
            self.lastSelectedGroup = self.activeGroup.title
        else:
            self.activeGroup = self.groupsByTitle[lsg]
            
        log.debug('onGroupSelect - group = %s' % safe_str(self.lastSelectedGroup))    
        self.renderPrograms()
        self.activeRenderToken = time.clock()
        
        if not self.activeGroup.postersDone:
            self.renderPosters(self.activeRenderToken, self.activeGroup)
        
        if not self.activeGroup.episodesDone:
            self.renderEpisodeColumn(self.activeRenderToken, self.activeGroup)
        
        if not self.activeGroup.backgroundsDone:
            self.renderBackgrounds(self.activeRenderToken, self.activeGroup)
        
    def renderPrograms(self):        
        @timed 
        def constructorTime():
            for p in self.activeGroup.programs:
                listItem = xbmcgui.ListItem()
                self.activeGroup.listItems.append(listItem)
                self.activeGroup.programsByListItem[listItem] = p
        
        @timed 
        def propertyTime(): 
            for i, p in enumerate(self.activeGroup.programs):
                try:
                    listItem = self.activeGroup.listItems[i]
                    self.setListItemProperty(listItem, 'title', p.fullTitle())
                    self.setListItemProperty(listItem, 'date', p.formattedAirDate())
                    self.setListItemProperty(listItem, 'time', p.formattedStartTime())
                    self.setListItemProperty(listItem, 'index', str(i+1))
                    
                    if self.fanArt.hasPosters(p):
                        p.needsPoster = False
                        self.lookupPoster(listItem, p)
                    else:
                        p.needsPoster = True
                        self.setListItemProperty(listItem, 'poster', 'loading.gif')
                except:
                    log.exception('Program = %s' % safe_str(p.fullTitle()))
        
        @timed
        def othertime():
            self.programsListbox.reset()
            self.programsListbox.addItems(self.activeGroup.listItems)
            # TODO: restore last selected -- self.programsListbox.selectItem(self.lastSelected)

        if not self.activeGroup.listItems:
            constructorTime()
            propertyTime()
        othertime()

    def lookupPoster(self, listItem, p):
        posterPath = self.fanArt.pickPoster(p)
        if not posterPath:
            posterPath = self.mythThumbnailCache.get(p)
            if not posterPath:
                posterPath = 'mythbox-logo.png'
        log.debug('lookupPoster setting %s tto %s' % (safe_str(p.title()), posterPath))
        self.updateListItemProperty(listItem, 'poster', posterPath)

        if log.isEnabledFor(logging.DEBUG):
            try:
                self.setListItemProperty(listItem, 'posterSize', formatSize(os.path.getsize(posterPath)/1000))
            except:
                pass

    def renderProgramDeleted2(self, deletedProgram, selectionIndex):
        savedLastSelectedGroupIndex = self.groupsByTitle[self.lastSelectedGroup].index
        
        title = deletedProgram.title()
        self.programs.remove(deletedProgram)
                
        for group in [self.groupsByTitle[title], self.groupsByTitle[self.allGroupTitle]]:
            log.debug('Removing title %s from group %s' % (safe_str(deletedProgram.fullTitle()), safe_str(group.title)))
            log.debug(group)
            group.programs.remove(deletedProgram)
            
            # update count in group
            self.updateListItemProperty(group.listItem, 'num_episodes', str(len(group.programs)))

            # if not rendered before, listItems will not have been realized
            if deletedProgram in group.programsByListItem.inv:
                listItem = group.programsByListItem[:deletedProgram]
                groupIndex = group.listItems.index(listItem)
                group.listItems.remove(listItem)
                del group.programsByListItem[listItem]

                #for i, listItem in enumerate(group.listItems[groupIndex:]):
                #    self.setListItemProperty(listItem, 'index', str(i + groupIndex + 1))

                self.programsListbox.reset()
                self.programsListbox.addItems(self.activeGroup.listItems)
            else:
                log.debug('Not removing listitem from group "%s" -- not realized' % safe_str(group.title))
                
            # if last program in group, nuke group
            if len(group.programs) == 0:
                log.debug('Group %s now empty -- removing group ' % safe_str(group.title))
                del self.groupsByTitle[group.title]

                # re-index
                for i,group in enumerate(self.groupsByTitle.values()):
                    group.index = i

                self.groupsListbox.reset()
                self.groupsListbox.addItems([group.listItem for group in self.groupsByTitle.values()])
            
        # next logical selection based on deleted program    
        try:
            if len(self.programs) == 0:
                # deleted last recording -- nothing to show
                self.setFocus(self.getControl(ID_REFRESH_BUTTON))
            elif self.lastSelectedGroup in self.groupsByTitle:
                log.debug("LSG %s not empty..selecting index %d" % (safe_str(self.lastSelectedGroup), savedLastSelectedGroupIndex))
                self.selectListItemAtIndex(self.groupsListbox, savedLastSelectedGroupIndex)
                self.selectListItemAtIndex(self.programsListbox, max(0, selectionIndex-1))
            else:
                log.debug("LSG %s is now empty..selected next best thing.." % self.lastSelectedGroup)
                newGroupIndex = savedLastSelectedGroupIndex - 1
                self.selectListItemAtIndex(self.groupsListbox, newGroupIndex)
                try:
                    newGroupTitle = [group.title for group in self.groupsByTitle.values() if group.index == newGroupIndex][0]
                except:
                    log.exception('determinging newGroupTitle blew up..selecting group All Recordings')
                    newGroupTitle = self.allGroupTitle
                self.lastSelectedGroup = newGroupTitle
                self.onGroupSelect(newGroupTitle)
                self.setFocus(self.groupsListbox)
        except Exception, e:
            log.warn(safe_str(e))
        
    @run_async
    @catchall
    def renderPosters(self, myRenderToken, myGroup):
        log.debug('renderPosters -- BEGIN --')
        for (listItem, program) in myGroup.programsByListItem.items()[:]:
            if self.closed or xbmc.abortRequested or myRenderToken != self.activeRenderToken: 
                return
            try:
                self.lookupPoster(listItem, program)
            except:
                log.exception('Program = %s' % safe_str(program.fullTitle()))
        myGroup.postersDone = True
        log.debug('renderPosters -- END --')

    @run_async
    @catchall
    def renderBackgrounds(self, myRenderToken, myGroup):
        try:
            log.debug('renderBackgrounds -- BEGIN --')
            for (listItem, program) in myGroup.programsByListItem.items()[:]:
                if self.closed or xbmc.abortRequested or myRenderToken != self.activeRenderToken: 
                    return
                try:
                    self.lookupBackground(listItem, program)
                except:
                    log.exception('renderBackground for Program = %s' % safe_str(program.fullTitle()))
            myGroup.backgroundsDone = True
        finally:
            log.debug('renderBackgrounds -- END --')

    def sameBackground(self, program):
        t = program.title()
        if not t in self.sameBackgroundCache:
            self.sameBackgroundCache[t] = self.fanArt.pickBackground(program)
        return self.sameBackgroundCache[t]

    def lookupBackground(self, listItem, p):
        path = self.sameBackground(p)
        if path is not None:
            log.debug('lookupBackground setting %s to %s' % (safe_str(p.title()), path))
            self.updateListItemProperty(listItem, 'background', path)

    @run_async
    @catchall
    def renderEpisodeColumn(self, myRenderToken, myGroup):
        results = odict.odict()
        for (listItem, program) in myGroup.programsByListItem.items()[:]:
            if self.closed or xbmc.abortRequested or myRenderToken != self.activeRenderToken:
                return
            try:
                season, episode = self.fanArt.getSeasonAndEpisode(program)
                if season and episode:
                    results[listItem] = '%sx%s' % (season, episode)
                    self.updateListItemProperty(listItem, 'episode', results[listItem])
            except:
                log.exception('Rendering season and episode for program %s' % safe_str(program.fullTitle()))
        myGroup.episodesDone = True
        
    def goRecordingDetails(self):
        self.lastSelected = self.programsListbox.getSelectedPosition()
        selectedItem = self.programsListbox.getSelectedItem()
        if not selectedItem:
            return
        
        selectedProgram = self.activeGroup.programsByListItem[selectedItem]
        if not selectedProgram:
            return
        
        programIterator = CyclingBidiIterator(self.activeGroup.programs, self.lastSelected)
        
        win = RecordingDetailsWindow(
            'mythbox_recording_details.xml', 
            self.platform.getScriptDir(), 
            forceFallback=True,
            programIterator=programIterator,
            settings=self.settings,
            translator=self.translator,
            platform=self.platform,
            cachesByName=self.cachesByName,
            fanArt=self.fanArt)
        win.doModal()

        if win.isDeleted:
            self.renderProgramDeleted2(programIterator.current(), programIterator.index())
        elif programIterator.index() != self.lastSelected:
            self.programsListbox.selectItem(programIterator.index())
                
        del win
