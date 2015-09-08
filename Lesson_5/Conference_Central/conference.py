#!/usr/bin/env python

"""
conference.py -- Udacity conference server-side Python App Engine API;
    uses Google Cloud Endpoints

$Id: conference.py,v 1.25 2014/05/24 23:42:19 wesc Exp wesc $

created by wesc on 2014 apr 21

"""

__author__ = 'wesc+api@google.com (Wesley Chun)'


from datetime import datetime

import endpoints
from protorpc import messages
from protorpc import message_types
from protorpc import remote

from google.appengine.api import memcache
from google.appengine.api import taskqueue
from google.appengine.api import memcache
from google.appengine.ext import ndb


from models import ConflictException
from models import Profile
from models import ProfileMiniForm
from models import ProfileForm
from models import BooleanMessage
from models import StringMessage
from models import Conference
from models import ConferenceForm
from models import ConferenceForms
from models import ConferenceQueryForms
from models import TeeShirtSize
from models import ConferenceSession
from models import ConferenceSessionForm
from models import ConferenceSessionForms
from models import Speaker
from models import SessionWishlist
from models import SessionWishlistForm

from utils import getUserId

from settings import WEB_CLIENT_ID

EMAIL_SCOPE = endpoints.EMAIL_SCOPE
API_EXPLORER_CLIENT_ID = endpoints.API_EXPLORER_CLIENT_ID
MEMCACHE_ANNOUNCEMENTS_KEY = "RECENT_ANNOUNCEMENTS"
MEMCACHE_FEATURED_SPEAKER_KEY = "FEATURED_SPEAKER"
MEMCACHE_FEATURED_SESSIONS_KEY = "FEATURED_SESSIONS"

# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -

DEFAULTS = {
    "city": "Default City",
    "maxAttendees": 0,
    "seatsAvailable": 0,
    "topics": [ "Default", "Topic" ],
}

OPERATORS = {
            'EQ':   '=',
            'GT':   '>',
            'GTEQ': '>=',
            'LT':   '<',
            'LTEQ': '<=',
            'NE':   '!='
            }

FIELDS =    {
            'CITY': 'city',
            'TOPIC': 'topics',
            # 'MONTH': 'month',
            'MAX_ATTENDEES': 'maxAttendees'
            }

CONF_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1),
)

CONF_POST_REQUEST = endpoints.ResourceContainer(
    ConferenceForm,
    websafeConferenceKey=messages.StringField(1),
)

SPEAKER_SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeSpeaker=messages.StringField(1)
)

CONF_SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeConferenceKey=messages.StringField(1)
)

TYPE_SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeType=messages.StringField(1),
    websafeConferenceKey=messages.StringField(2)
)

HIGHLIGHT_SESS_GET_REQUEST = endpoints.ResourceContainer(
    message_types.VoidMessage,
    websafeHighlight=messages.StringField(1)
)
# - - - - - - - - - - - - - - - - - - - - - - - - - - - - - -


@endpoints.api(name='conference', version='v1', 
    allowed_client_ids=[WEB_CLIENT_ID, API_EXPLORER_CLIENT_ID],
    scopes=[EMAIL_SCOPE])
class ConferenceApi(remote.Service):
    """Conference API v0.1"""

# - - - Conference objects - - - - - - - - - - - - - - - - -

    def _copyConferenceToForm(self, conf, displayName):
        """Copy relevant fields from Conference to ConferenceForm."""
        cf = ConferenceForm()
        for field in cf.all_fields():
            if hasattr(conf, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(cf, field.name, str(getattr(conf, field.name)))
                else:
                    setattr(cf, field.name, getattr(conf, field.name))
            elif field.name == "websafeKey":
                setattr(cf, field.name, conf.key.urlsafe())
        if displayName:
            setattr(cf, 'organizerDisplayName', displayName)
        cf.check_initialized()
        return cf


    def _createConferenceObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        if not request.name:
            raise endpoints.BadRequestException("Conference 'name' field required")

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']
        del data['month']

        # add default values for those missing (both data model & outbound Message)
        for df in DEFAULTS:
            if data[df] in (None, []):
                data[df] = DEFAULTS[df]
                setattr(request, df, DEFAULTS[df])

        # convert dates from strings to Date objects; set month based on start_date
        if data['startDate']:
            data['startDate'] = datetime.strptime(data['startDate'][:10], "%Y-%m-%d").date()

        if data['endDate']:
            data['endDate'] = datetime.strptime(data['endDate'][:10], "%Y-%m-%d").date()

        # set seatsAvailable to be same as maxAttendees on creation
        if data["maxAttendees"] > 0:
            data["seatsAvailable"] = data["maxAttendees"]
        # generate Profile Key based on user ID and Conference
        # ID based on Profile key get Conference key from ID
        p_key = ndb.Key(Profile, user_id)
        c_id = Conference.allocate_ids(size=1, parent=p_key)[0]
        c_key = ndb.Key(Conference, c_id, parent=p_key)
        data['key'] = c_key
        data['organizerUserId'] = request.organizerUserId = user_id
        # TODO 2
        # create Conference, send email to organizer confirming
        # creation of Conference & return (modified) ConferenceForm
        Conference(**data).put()
        taskqueue.add(params={'email': user.email(),
                              'conferenceInfo': repr(request)},
                      url='/tasks/send_confirmation_email'
                      )

        return request


    @ndb.transactional()
    def _updateConferenceObject(self, request):
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id = getUserId(user)

        # copy ConferenceForm/ProtoRPC Message into dict
        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['websafeKey']
        del data['organizerDisplayName']

        # update existing conference
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        # check that conference exists
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)

        # check that user is owner
        if user_id != conf.organizerUserId:
            raise endpoints.ForbiddenException(
                'Only the owner can update the conference.')

        # Not getting all the fields, so don't create a new object; just
        # copy relevant fields from ConferenceForm to Conference object
        for field in request.all_fields():
            data = getattr(request, field.name)
            # only copy fields where we get data
            if data not in (None, []):
                # special handling for dates (convert string to Date)
                if field.name in ('startDate', 'endDate'):
                    data = datetime.strptime(data, "%Y-%m-%d").date()
                # write to Conference object
                setattr(conf, field.name, data)
        conf.put()
        prof = ndb.Key(Profile, user_id).get()
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(ConferenceForm, ConferenceForm, path='conference',
            http_method='POST', name='createConference')
    def createConference(self, request):
        """Create new conference."""
        return self._createConferenceObject(request)


    @endpoints.method(CONF_POST_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='PUT', name='updateConference')
    def updateConference(self, request):
        """Update conference w/provided fields & return w/updated info."""
        return self._updateConferenceObject(request)


    @endpoints.method(CONF_GET_REQUEST, ConferenceForm,
            path='conference/{websafeConferenceKey}',
            http_method='GET', name='getConference')
    def getConference(self, request):
        """Return requested conference (by websafeConferenceKey)."""
        # get Conference object from request; bail if not found
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % request.websafeConferenceKey)
        prof = conf.key.parent().get()
        # return ConferenceForm
        return self._copyConferenceToForm(conf, getattr(prof, 'displayName'))


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='getConferencesCreated',
            http_method='POST', name='getConferencesCreated')
    def getConferencesCreated(self, request):
        """Return conferences created by user."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')
        user_id =  getUserId(user)
        # create ancestor query for all key matches for this user
        confs = Conference.query(ancestor=ndb.Key(Profile, user_id))
        prof = ndb.Key(Profile, user_id).get()
        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, getattr(prof, 'displayName')) for conf in confs]
        )


    def _getQuery(self, request):
        """Return formatted query from the submitted filters."""
        q = Conference.query()
        inequality_filter, filters = self._formatFilters(request.filters)

        # If exists, sort on inequality filter first
        if not inequality_filter:
            q = q.order(Conference.name)
        else:
            q = q.order(ndb.GenericProperty(inequality_filter))
            q = q.order(Conference.name)

        for filtr in filters:
            if filtr["field"] in ["month", "maxAttendees"]:
                filtr["value"] = int(filtr["value"])
            formatted_query = ndb.query.FilterNode(filtr["field"], filtr["operator"], filtr["value"])
            q = q.filter(formatted_query)
        return q


    def _formatFilters(self, filters):
        """Parse, check validity and format user supplied filters."""
        formatted_filters = []
        inequality_field = None

        for f in filters:
            filtr = {field.name: getattr(f, field.name) for field in f.all_fields()}

            try:
                filtr["field"] = FIELDS[filtr["field"]]
                filtr["operator"] = OPERATORS[filtr["operator"]]
            except KeyError:
                raise endpoints.BadRequestException("Filter contains invalid field or operator.")

            # Every operation except "=" is an inequality
            if filtr["operator"] != "=":
                # check if inequality operation has been used in previous filters
                # disallow the filter if inequality was performed on a different field before
                # track the field on which the inequality operation is performed
                if inequality_field and inequality_field != filtr["field"]:
                    raise endpoints.BadRequestException("Inequality filter is allowed on only one field.")
                else:
                    inequality_field = filtr["field"]

            formatted_filters.append(filtr)
        return (inequality_field, formatted_filters)


    @endpoints.method(ConferenceQueryForms, ConferenceForms,
            path='queryConferences',
            http_method='POST',
            name='queryConferences')
    def queryConferences(self, request):
        """Query for conferences."""
        conferences = self._getQuery(request)

        # need to fetch organiser displayName from profiles
        # get all keys and use get_multi for speed
        organisers = [(ndb.Key(Profile, conf.organizerUserId)) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return individual ConferenceForm object per Conference
        return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in \
                conferences]
        )

#-----------WISHLIST-----------------------------------------------

    @endpoints.method(SessionWishlistForm, SessionWishlistForm,
                      path='addSessionToWishlist',
                      http_method='POST',
                      name='addSessionToWishlist')
    def addSessionToWishlist(self, request):
        """adds the session to the user's list of sessions they are interested in attending

        """
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # retrieve the session
        sess = ndb.Key(urlsafe=request.session_key).get()

        if sess is None:
            raise endpoints.NotFoundException(
                "No session for the given key was found"
            )

        # retrieve the user's wishlist if one exists
        wl = ndb.Key(SessionWishlist, getUserId(user)).get()
        if wl is None:
            wl_key = ndb.Key(SessionWishlist, getUserId(user))
            wl = SessionWishlist(
                key=wl_key,
                session_keys=[request.session_key]
            )
            wl.put()
            return request
        # if the wishlist exists then just add the sess to it
        wl_sess_keys = wl.session_keys
        if request.session_key not in wl.session_keys:
            wl_sess_keys.append(request.session_key)
            wl.session_keys = wl_sess_keys
            wl.put()
        wl.key.delete()
        return SessionWishlistForm(
            session_key=request.session_key
        )

    @endpoints.method(message_types.VoidMessage, ConferenceSessionForms,
                      path='getSessionsInWishlist',
                      http_method='GET',
                      name='getSessionsInWishlist')
    def getSessionsInWishlist(self, request):
        """query for all the sessions in a conference that the user is interested in"""
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        wl = ndb.Key(SessionWishlist, getUserId(user)).get()
        if wl is not None:
            sess_keys = [ndb.Key(urlsafe=wl_sess_key) for wl_sess_key in wl.session_keys]
            q = ndb.get_multi(sess_keys)
            return ConferenceSessionForms(
                items=[self._copySessionToForm(sess) for sess in q]
            )
        return ConferenceSessionForms(
            items=[]
        )

# - - - Session Stuff - - - - - - - - - - - - - - - - - - - -
    def _copySessionToForm(self, sess):
        """Copy the session to the session form"""
        sf = ConferenceSessionForm()
        for field in sf.all_fields():
            if hasattr(sess, field.name):
                # convert Date to date string; just copy others
                if field.name.endswith('Date'):
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                elif field.name == 'speakers':
                    setattr(sf, field.name, [s.name for s in getattr(sess, field.name)])
                elif field.name == 'start_time':
                    setattr(sf, field.name, str(getattr(sess, field.name)))
                else:
                    setattr(sf, field.name, getattr(sess, field.name))
            elif field.name == "websafeKey":
                setattr(sf, field.name, sess.key.urlsafe())
        sf.check_initialized()
        return sf

    #---------Custom query #1--------------------------------------
    @endpoints.method(HIGHLIGHT_SESS_GET_REQUEST, ConferenceSessionForms,
                      path='getConferenceSessionsByHighlight/{websafeHighlight}',
                      http_method='GET',
                      name='getConferenceSessionsByHighlight')
    def getConferenceSessionsByHighlight(self, request):
        """Query for sessions with the given highlight"""
        q = ConferenceSession.query(ConferenceSession.highlights.IN([request.websafeHighlight]))
        if q is not None:
            return ConferenceSessionForms(
                items=[self._copySessionToForm(sess) for sess in q]
            )
        return ConferenceSessionForms(
            items=[]
        )

    #---------Task #3 filter query---------------------------------
    @endpoints.method(message_types.VoidMessage, ConferenceSessionForms,
                      path='getConferenceSessionsByFilters',
                      http_method='GET',
                      name='getConferenceSessionsByFilters')
    def getConferenceSessionsByFilters(self, request):
        """Query for the sessions that are not specified type
        and are not running after specified time
        """
        # first get the sessions that don't have the specified type
        # then get the sessions that don't run past the specified time
        type_q = ConferenceSession.query(ConferenceSession.type != 'Workshop')
        sessions=[]
        for sess in type_q:
            if sess.start_time is not None:
                if sess.start_time < datetime.strptime('19:00', '%H:%M').time():
                    # add the session to the sessions
                    sessions.append(sess)
        return ConferenceSessionForms(
            items=[self._copySessionToForm(s) for s in sessions]
        )

    @endpoints.method(CONF_SESS_GET_REQUEST, ConferenceSessionForms,
                      path='getConferenceSessions/{websafeConferenceKey}',
                      http_method='GET',
                      name='getConferenceSessions')
    def getConferenceSessions(self, request):
        """Query for sessions, given a conference"""
        # query for the conference, and then query for the sessions by conference key
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        q = ConferenceSession.query(ancestor=conf.key)
        q.order(ConferenceSession.start_time)
        return ConferenceSessionForms(
            items=[self._copySessionToForm(sess) for sess in q]
        )

    @endpoints.method(TYPE_SESS_GET_REQUEST, ConferenceSessionForms,
                      path='sessiontype/{websafeConferenceKey}/{websafeType}',
                      http_method='GET',
                      name='getConferenceSessionsByType')
    def getConferenceSessionsByType(self, request):
        """Query for sessions, given the type"""
        conf = ndb.Key(urlsafe=request.websafeConferenceKey).get()
        q = ConferenceSession.query(ancestor=conf.key)
        q = q.filter(ConferenceSession.type == request.websafeType)
        q.order(ConferenceSession.start_time)
        return ConferenceSessionForms(
            items=[self._copySessionToForm(sess) for sess in q]
        )

    @endpoints.method(SPEAKER_SESS_GET_REQUEST, ConferenceSessionForms,
                      path='sessionspeaker/{websafeSpeaker}',
                      http_method='GET',
                      name='getSessionsBySpeaker')
    def getSessionsBySpeaker(self, request):
        """Query for sessions with a given speaker"""
        q = ConferenceSession.query(ConferenceSession.speakers.name == request.websafeSpeaker)
        q.order(ConferenceSession.start_time)
        return ConferenceSessionForms(
            items=[self._copySessionToForm(sess) for sess in q]
        )

    def _createSessionObject(self, request):
        """Create or update Conference object, returning ConferenceForm/request."""
        # preload necessary data items
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        if not request.parent_key:
            raise endpoints.BadRequestException("Session 'parent_key' field required")

        data = {field.name: getattr(request, field.name) for field in request.all_fields()}
        del data['parent_key']
        if data['start_time']:
            # assumed that start time is given in 24 hour time :)
            t = datetime.strptime(data['start_time'], '%H:%M')
            t = t.time()
            data['start_time'] = t
        if data['date']:
            data['date'] = datetime.strptime(data['date'][:10], "%Y-%m-%d").date()
        data['speakers'] = []
        for s in request.speakers:
            data['speakers'].append(Speaker(name=s))

        conf = ndb.Key(urlsafe=request.parent_key).get()
        sess = ConferenceSession(parent=conf.key, **data)

        sess.put()
        # start the task to find a featured speaker
        # if there are multiple speakers check each one
        for speaker in data['speakers']:
            taskqueue.add(params={'conf_key': request.parent_key,
                                  'speaker_name': speaker.name},
                          url='/tasks/set_featured_speaker'
                          )

        return request

    @endpoints.method(ConferenceSessionForm, ConferenceSessionForm, path='createSession',
                      http_method='POST', name='createSession')
    def createSession(self, request):
        """Create a Session. Requires the conference key passed in."""
        return self._createSessionObject(request)

# - - - Profile objects - - - - - - - - - - - - - - - - - - -

    def _copyProfileToForm(self, prof):
        """Copy relevant fields from Profile to ProfileForm."""
        # copy relevant fields from Profile to ProfileForm
        pf = ProfileForm()
        for field in pf.all_fields():
            if hasattr(prof, field.name):
                # convert t-shirt string to Enum; just copy others
                if field.name == 'teeShirtSize':
                    setattr(pf, field.name, getattr(TeeShirtSize, getattr(prof, field.name)))
                else:
                    setattr(pf, field.name, getattr(prof, field.name))
        pf.check_initialized()
        return pf


    def _getProfileFromUser(self):
        """Return user Profile from datastore, creating new one if non-existent."""
        # make sure user is authed
        user = endpoints.get_current_user()
        if not user:
            raise endpoints.UnauthorizedException('Authorization required')

        # get Profile from datastore
        user_id = getUserId(user)
        p_key = ndb.Key(Profile, user_id)
        profile = p_key.get()
        # create new Profile if not there
        if not profile:
            profile = Profile(
                key = p_key,
                displayName = user.nickname(),
                mainEmail= user.email(),
                teeShirtSize = str(TeeShirtSize.NOT_SPECIFIED),
            )
            profile.put()

        return profile      # return Profile


    def _doProfile(self, save_request=None):
        """Get user Profile and return to user, possibly updating it first."""
        # get user Profile
        prof = self._getProfileFromUser()

        # if saveProfile(), process user-modifyable fields
        if save_request:
            for field in ('displayName', 'teeShirtSize'):
                if hasattr(save_request, field):
                    val = getattr(save_request, field)
                    if val:
                        setattr(prof, field, str(val))
                        #if field == 'teeShirtSize':
                        #    setattr(prof, field, str(val).upper())
                        #else:
                        #    setattr(prof, field, val)
            prof.put()

        # return ProfileForm
        return self._copyProfileToForm(prof)


    @endpoints.method(message_types.VoidMessage, ProfileForm,
            path='profile', http_method='GET', name='getProfile')
    def getProfile(self, request):
        """Return user profile."""
        return self._doProfile()


    @endpoints.method(ProfileMiniForm, ProfileForm,
            path='profile', http_method='POST', name='saveProfile')
    def saveProfile(self, request):
        """Update & return user profile."""
        return self._doProfile(request)


# - - - Registration - - - - - - - - - - - - - - - - - - - -

    @ndb.transactional(xg=True)
    def _conferenceRegistration(self, request, reg=True):
        """Register or unregister user for selected conference."""
        retval = None
        prof = self._getProfileFromUser() # get user Profile

        # check if conf exists given websafeConfKey
        # get conference; check that it exists
        wsck = request.websafeConferenceKey
        conf = ndb.Key(urlsafe=wsck).get()
        if not conf:
            raise endpoints.NotFoundException(
                'No conference found with key: %s' % wsck)

        # register
        if reg:
            # check if user already registered otherwise add
            if wsck in prof.conferenceKeysToAttend:
                raise ConflictException(
                    "You have already registered for this conference")

            # check if seats avail
            if conf.seatsAvailable <= 0:
                raise ConflictException(
                    "There are no seats available.")

            # register user, take away one seat
            prof.conferenceKeysToAttend.append(wsck)
            conf.seatsAvailable -= 1
            retval = True

        # unregister
        else:
            # check if user already registered
            if wsck in prof.conferenceKeysToAttend:

                # unregister user, add back one seat
                prof.conferenceKeysToAttend.remove(wsck)
                conf.seatsAvailable += 1
                retval = True
            else:
                retval = False

        # write things back to the datastore & return
        prof.put()
        conf.put()
        return BooleanMessage(data=retval)


    @endpoints.method(message_types.VoidMessage, ConferenceForms,
            path='conferences/attending',
            http_method='GET', name='getConferencesToAttend')
    def getConferencesToAttend(self, request):
        """Get list of conferences that user has registered for."""
        prof = self._getProfileFromUser() # get user Profile
        conf_keys = [ndb.Key(urlsafe=wsck) for wsck in prof.conferenceKeysToAttend]
        conferences = ndb.get_multi(conf_keys)

        # get organizers
        organisers = [ndb.Key(Profile, conf.organizerUserId) for conf in conferences]
        profiles = ndb.get_multi(organisers)

        # put display names in a dict for easier fetching
        names = {}
        for profile in profiles:
            names[profile.key.id()] = profile.displayName

        # return set of ConferenceForm objects per Conference
        return ConferenceForms(
            items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in conferences]
        )

    #---------Custom query #2--------------------------------------
    @endpoints.method(HIGHLIGHT_SESS_GET_REQUEST, ConferenceForms,
                      path='getConferencesWithSessionHighlights/{websafeHighlight}',
                      http_method='GET',
                      name='getConferencesWithSessionHighlights')
    def getConferencesWithSessionHighlights(self, request):
        """Query for conferences that have a session with the given highlights"""
        q = ConferenceSession.query(ConferenceSession.highlights.IN([request.websafeHighlight]))

        if q is not None:
            # grab the confs for the sessions
            confs = [sess.key.parent().get() for sess in q]

            # grab organizers of each conference
            organizers = [ndb.Key(Profile, conf.organizerUserId) for conf in confs]
            profiles = ndb.get_multi(organizers)

            # put display names in dict
            names = {}
            for profile in profiles:
                names[profile.key.id()] = profile.displayName

            return ConferenceForms(
                items=[self._copyConferenceToForm(conf, names[conf.organizerUserId]) for conf in confs]
            )
        return ConferenceForms(
            items=[]
        )

    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='POST', name='registerForConference')
    def registerForConference(self, request):
        """Register user for selected conference."""
        return self._conferenceRegistration(request)


    @endpoints.method(CONF_GET_REQUEST, BooleanMessage,
            path='conference/{websafeConferenceKey}',
            http_method='DELETE', name='unregisterFromConference')
    def unregisterFromConference(self, request):
        """Unregister user for selected conference."""
        return self._conferenceRegistration(request, reg=False)


# - - - Announcements - - - - - - - - - - - - - - - - - - - -

    @staticmethod
    def _cacheAnnouncement():
        """Create Announcement & assign to memcache; used by
        memcache cron job & putAnnouncement().
        """
        confs = Conference.query(ndb.AND(
            Conference.seatsAvailable <= 5,
            Conference.seatsAvailable > 0)
        ).fetch(projection=[Conference.seatsAvailable, Conference.name])

        if confs:
            # If there are almost sold out conferences,
            # format announcement and set it in memcache
            announcement = '%s %s' % (
                'Last chance to attend! The following conferences '
                'are nearly sold out:',
                ', '.join(conf.name for conf in confs))
            memcache.set(MEMCACHE_ANNOUNCEMENTS_KEY, announcement)
        else:
            # If there are no sold out conferences,
            # delete the memcache announcements entry
            announcement = ""
            memcache.delete(MEMCACHE_ANNOUNCEMENTS_KEY)

        return announcement


    @endpoints.method(message_types.VoidMessage, StringMessage,
                      path='conference/announcement/get',
                      http_method='GET', name='getAnnouncement')
    def getAnnouncement(self, request):
        """Return Announcement from memcache."""
        # TODO 1
        # return an existing announcement from Memcache or an empty string.
        announcement = memcache.get(MEMCACHE_ANNOUNCEMENTS_KEY)
        if announcement is None:
            announcement = ""
        return StringMessage(data=announcement)

#---------Task #4--------------------------------------------

    @staticmethod
    def _setFeaturedSpeaker(conference_key, speaker_name):
        """ Checks whether the speaker is a featured speaker or not
        and if they are, will set them into memcache with the sessions
        they speak in
        :param conference_key: the conference key
        :param speaker_name: the speaker name
        """
        conf = ndb.Key(urlsafe=conference_key).get()
        q = ConferenceSession.query(ancestor=conf.key)
        q = q.filter(ConferenceSession.speakers.name.IN([speaker_name]))
        session_names=[sess.name for sess in q]
        if q.count(limit=2) > 1:
            memcache.set(MEMCACHE_FEATURED_SPEAKER_KEY, speaker_name)
            memcache.set(MEMCACHE_FEATURED_SESSIONS_KEY, session_names)


    @endpoints.method(message_types.VoidMessage, StringMessage,
                      path='conference/featured_speaker/get',
                      http_method='GET', name='getFeaturedSpeaker')
    def getFeaturedSpeaker(self, request):
        """Returns the featured speaker for the conference and
        the names of the speaker's sessions as a string
        """
        featuredSpeaker = memcache.get(MEMCACHE_FEATURED_SPEAKER_KEY)
        if featuredSpeaker is None:
            return StringMessage(data="No featured speaker is available")
        featString = "%s is speaking at the following sessions: %s" % (
            featuredSpeaker,
            ', '.join(sessName for sessName in memcache.get(MEMCACHE_FEATURED_SESSIONS_KEY)))
        return StringMessage(data=featString)

api = endpoints.api_server([ConferenceApi]) # register API
